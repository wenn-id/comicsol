use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io::Read;

use serde_json::{Value, json};

use super::{Report, STORYBOARD, artifact, bytes_hash, is_sha256, items, keys, number_equal, text};
use super::{panels, planning, storyboard};
use crate::{manifest, project_fs};

const CACHE: &str = "cache/composition.json";
const RECORD_FIELDS: &[&str] = &[
    "bindings",
    "checks",
    "decision",
    "kind",
    "review",
    "schema_version",
    "subject_id",
    "unresolved_warnings",
];
const BINDINGS: &[&str] = &[
    "composition_cache_path",
    "composition_cache_sha256",
    "layout_name",
    "layout_version",
    "lettering_sha256s",
    "normalization_sha256s",
    "page_height",
    "page_path",
    "page_sha256",
    "page_width",
    "storyboard_path",
    "storyboard_sha256",
];
const CHECKS: &[&str] = &[
    "clipped-text",
    "text-overlap",
    "face-action-obstruction",
    "bubble-tail-direction",
    "reading-order",
    "accidental-text-watermark",
    "layout-border-integrity",
    "balloon-subject-obstruction",
    "bubble-tail-geometry",
    "balloon-crowding",
];
const DETERMINISTIC: &[&str] = &[
    "clipped-text",
    "text-overlap",
    "reading-order",
    "layout-border-integrity",
    "balloon-subject-obstruction",
    "bubble-tail-geometry",
    "balloon-crowding",
];
const PANEL_BINDINGS: &[(&str, &str, &str)] = &[
    ("lettering_sha256s", "lettering.json", "lettering"),
    (
        "normalization_sha256s",
        "normalization.json",
        "normalization",
    ),
];
const MAX_ARTIFACT: u64 = 128 * 1024 * 1024;
const MAX_JSON: usize = crate::error::contract::MAX_JSON_BYTES;

struct Snapshot {
    digest: String,
    value: Option<Value>,
    dimensions: Option<[u32; 2]>,
}

struct Snapshots<'a> {
    root: &'a File,
    page: &'a str,
    read: HashMap<String, Option<Snapshot>>,
}

impl Snapshots<'_> {
    fn get(&mut self, path: &str) -> Option<&Snapshot> {
        self.read
            .entry(path.to_owned())
            .or_insert_with(|| {
                let (dir, name) = artifact(self.root, path).ok()??;
                let file = project_fs::open_file(&dir, &name, false, false).ok()?;
                if file.metadata().ok()?.len() > MAX_ARTIFACT {
                    return None;
                }
                let mut bytes = Vec::new();
                file.take(MAX_ARTIFACT + 1).read_to_end(&mut bytes).ok()?;
                if bytes.len() as u64 > MAX_ARTIFACT {
                    return None;
                }
                let value = if path.ends_with(".json") && bytes.len() <= MAX_JSON {
                    std::str::from_utf8(&bytes)
                        .ok()
                        .and_then(|source| manifest::parse_document(source, path).ok())
                        .filter(Value::is_object)
                } else {
                    None
                };
                let dimensions = if path == self.page {
                    panels::decode_bytes(&bytes)
                        .ok()
                        .map(|raster| [raster.width, raster.height])
                } else {
                    None
                };
                Some(Snapshot {
                    digest: bytes_hash(&bytes),
                    value,
                    dimensions,
                })
            })
            .as_ref()
    }
}

fn stale(report: &mut Report, field: &str, detail: &str) {
    report.add(field, format!("page-quality-stale: {detail}"));
}

pub(super) fn inspect(root: &File, page: usize, record: &Value, report: &mut Report) {
    inspect_with_page(root, page, record, None, report);
}

pub(super) fn inspect_with_page(
    root: &File,
    page: usize,
    record: &Value,
    captured: Option<(&str, [u32; 2])>,
    report: &mut Report,
) {
    let start = report.issues.len();
    if !record.is_object() {
        stale(
            report,
            "record.path",
            "page quality record is missing or unreadable",
        );
        return;
    }
    for field in record
        .as_object()
        .into_iter()
        .flat_map(|value| value.keys())
    {
        if !RECORD_FIELDS.contains(&field.as_str()) {
            stale(report, field, "unknown page quality record field");
        }
    }
    for field in RECORD_FIELDS {
        if record.get(field).is_none() {
            stale(
                report,
                field,
                "required page quality record field is missing",
            );
        }
    }
    if record["schema_version"] != "2.1" || record["kind"] != "page-qa" {
        stale(
            report,
            "schema_version",
            "page quality record is not schema 2.1",
        );
        return;
    }
    let subject = format!("page-{page:03}");
    if record["subject_id"] != subject {
        stale(report, "subject_id", "page subject does not match its path");
    }
    inspect_checks(record, report);
    let bindings = &record["bindings"];
    let Some(fields) = bindings.as_object() else {
        stale(report, "bindings", "page quality bindings are missing");
        return;
    };
    if fields
        .keys()
        .any(|field| !BINDINGS.contains(&field.as_str()))
    {
        stale(
            report,
            "bindings",
            "page quality bindings contain unknown fields",
        );
    }
    for field in BINDINGS {
        if !fields.contains_key(*field) {
            stale(
                report,
                &format!("bindings.{field}"),
                "required page quality binding is missing",
            );
        }
    }
    let image = format!("pages/{subject}.png");
    let mut snapshots = Snapshots {
        root,
        page: &image,
        read: HashMap::new(),
    };
    if let Some((digest, dimensions)) = captured {
        snapshots.read.insert(
            image.clone(),
            Some(Snapshot {
                digest: digest.to_owned(),
                value: None,
                dimensions: Some(dimensions),
            }),
        );
    }
    for (path_field, hash_field) in [
        ("page_path", "page_sha256"),
        ("composition_cache_path", "composition_cache_sha256"),
        ("storyboard_path", "storyboard_sha256"),
    ] {
        let path = bindings[path_field].as_str();
        if path.is_none() {
            stale(
                report,
                &format!("bindings.{path_field}"),
                "bound artifact path must be a string",
            );
        }
        let hash_key = format!("bindings.{hash_field}");
        if !bindings[hash_field].as_str().is_some_and(is_sha256) {
            stale(
                report,
                &hash_key,
                "bound artifact hash must be a lowercase SHA-256",
            );
        }
        match path.and_then(|path| snapshots.get(path)) {
            None => stale(report, &hash_key, "bound artifact is missing"),
            Some(snapshot) if bindings[hash_field] != snapshot.digest => {
                stale(report, &hash_key, "bound artifact hash does not match");
            }
            Some(_) => {}
        }
    }
    for field in ["page_width", "page_height"] {
        if bindings[field].as_u64().is_none_or(|number| number == 0) {
            stale(
                report,
                &format!("bindings.{field}"),
                "bound page dimension must be a positive integer",
            );
        }
    }
    for (field, label) in [("layout_name", "name"), ("layout_version", "version")] {
        if bindings[field].as_str().is_none_or(str::is_empty) {
            stale(
                report,
                &format!("bindings.{field}"),
                &format!("bound layout {label} must be non-empty"),
            );
        }
    }
    for &(field, filename, label) in PANEL_BINDINGS {
        let key = format!("bindings.{field}");
        let Some(recorded) = bindings[field].as_array() else {
            stale(
                report,
                &key,
                &format!("ordered {label} bindings are missing"),
            );
            continue;
        };
        let current = recorded
            .iter()
            .map(|binding| {
                let (panel, _) = binding.as_str()?.split_once(':')?;
                let snapshot = snapshots.get(&format!("panels/{panel}/{filename}"))?;
                Some(Value::String(format!("{panel}:{}", snapshot.digest)))
            })
            .collect::<Option<Vec<_>>>();
        if current.as_ref() != Some(recorded) {
            stale(
                report,
                &key,
                &format!("ordered {label} artifact hashes do not match"),
            );
        }
    }
    let Some((current, panels, lettering)) = current_bindings(&mut snapshots, page) else {
        if report.issues.len() == start {
            stale(
                report,
                "bindings",
                "current page provenance is missing or unreadable",
            );
        }
        return;
    };
    if let Some(checks) = record["checks"].as_array()
        && let Err(detail) = tail_evidence(checks, &panels, &lettering)
    {
        stale(
            report,
            "checks",
            &format!("bubble-tail-evidence-mismatch: {detail}"),
        );
    }
    for field in BINDINGS {
        if !equal(&bindings[field], &current[field]) {
            stale(
                report,
                &format!("bindings.{field}"),
                "bound value does not match current artifacts",
            );
        }
    }
}

fn inspect_checks(record: &Value, report: &mut Report) {
    for category in panels::check_categories(&record["checks"], CHECKS) {
        stale(report, "checks", category);
    }
    let checks = items(&record["checks"]);
    let deterministic: Vec<_> = checks
        .iter()
        .filter(|check| {
            check["id"]
                .as_str()
                .is_some_and(|id| DETERMINISTIC.contains(&id))
        })
        .collect();
    if deterministic.iter().any(|check| {
        check["result"] == "pass" && !check["regions"].as_array().is_some_and(Vec::is_empty)
    }) {
        stale(
            report,
            "checks",
            "deterministic passing checks must not include failure regions",
        );
    }
    if deterministic.iter().any(|check| {
        matches!(check["result"].as_str(), Some("warning" | "fail"))
            && check["regions"].as_array().is_some_and(Vec::is_empty)
    }) {
        stale(
            report,
            "checks",
            "deterministic failing checks must include failure regions",
        );
    }
    let review = &record["review"];
    if !review.is_object() {
        stale(report, "review", "page quality review is missing");
    } else {
        if !keys(review, &["method", "reviewer", "reviewed_at"]) {
            stale(report, "review", "page quality review fields are invalid");
        }
        if review["method"] != "deterministic-plus-bounded-visual-review" {
            stale(
                report,
                "review.method",
                "page quality review method is invalid",
            );
        }
        if text(&review["reviewer"]).is_none() {
            stale(
                report,
                "review.reviewer",
                "page quality reviewer is missing",
            );
        }
        if !planning::valid_timestamp(&review["reviewed_at"]) {
            stale(
                report,
                "review.reviewed_at",
                "page quality review timestamp is invalid",
            );
        }
    }
    if record["checks"].is_array() {
        let failures = checks
            .iter()
            .any(|check| check["result"] == "fail" && check["severity"] == "error");
        let warnings: Vec<_> = checks
            .iter()
            .filter(|check| check["result"] == "warning" || check["severity"] == "warning")
            .map(|check| check["evidence"].clone())
            .collect();
        let decision = if failures {
            "regenerate"
        } else if warnings.is_empty() {
            "accept"
        } else {
            "accept-warning"
        };
        if record["decision"] != decision {
            stale(
                report,
                "decision",
                "page quality decision does not match checks",
            );
        }
        if record["unresolved_warnings"] != Value::Array(warnings) {
            stale(
                report,
                "unresolved_warnings",
                "page quality warnings do not match checks",
            );
        }
    }
}

fn current_bindings(
    snapshots: &mut Snapshots,
    page: usize,
) -> Option<(Value, Vec<Value>, BTreeMap<String, Value>)> {
    let board_snapshot = snapshots.get(STORYBOARD)?;
    let board_hash = board_snapshot.digest.clone();
    let mut matching = items(&board_snapshot.value.as_ref()?["pages"])
        .iter()
        .filter(|item| number_equal(&item["number"], page));
    let board_page = matching.next()?;
    if matching.next().is_some() {
        return None;
    }
    let panels = board_page["panels"].as_array()?.clone();
    if panels.is_empty() {
        return None;
    }
    let matched_layout = storyboard::match_layout(&panels)?;
    let layout = board_page["layout"]
        .as_str()
        .filter(|layout| !layout.is_empty())
        .unwrap_or(matched_layout)
        .to_owned();
    let page_path = snapshots.page;
    let page_snapshot = snapshots.get(page_path)?;
    let page_hash = page_snapshot.digest.clone();
    let [width, height] = page_snapshot.dimensions?;
    let cache_hash = snapshots.get(CACHE)?.digest.clone();
    let mut lettering = BTreeMap::new();
    let mut lettering_hashes = Vec::new();
    let mut normalization_hashes = Vec::new();
    for panel in &panels {
        let id = panel["id"].as_str()?;
        let snapshot = snapshots.get(&format!("panels/{id}/lettering.json"))?;
        lettering_hashes.push(format!("{id}:{}", snapshot.digest));
        lettering.insert(id.to_owned(), snapshot.value.clone()?);
        let normalization = snapshots.get(&format!("panels/{id}/normalization.json"))?;
        let [w, h] = items(&normalization.value.as_ref()?["clean"]["size"]) else {
            return None;
        };
        if [w, h]
            .iter()
            .any(|number| number.as_u64().is_none_or(|value| value == 0))
        {
            return None;
        }
        normalization_hashes.push(format!("{id}:{}", normalization.digest));
    }
    let current = json!({
        "composition_cache_path": CACHE, "composition_cache_sha256": cache_hash,
        "layout_name": layout, "layout_version": "1",
        "lettering_sha256s": lettering_hashes, "normalization_sha256s": normalization_hashes,
        "page_height": height, "page_path": page_path, "page_sha256": page_hash, "page_width": width,
        "storyboard_path": STORYBOARD, "storyboard_sha256": board_hash,
    });
    Some((current, panels, lettering))
}

fn tail_evidence(
    checks: &[Value],
    panels: &[Value],
    lettering: &BTreeMap<String, Value>,
) -> Result<(), &'static str> {
    let check = checks
        .iter()
        .find(|check| check["id"] == "bubble-tail-direction")
        .ok_or("check is missing")?;
    let regions = check["regions"]
        .as_array()
        .ok_or("regions must be an array")?;
    let mut expected = BTreeMap::new();
    for panel in panels {
        let panel_id = panel["id"].as_str().ok_or("panel is invalid")?;
        let geometry = lettering
            .get(panel_id)
            .and_then(|value| value["items"].as_array())
            .ok_or("lettering geometry is missing")?;
        let text = panel["text"].as_array().ok_or("panel text is invalid")?;
        for item in text.iter().filter(|item| item["kind"] == "dialogue") {
            let id = item["id"].as_str().ok_or("dialogue tail is missing")?;
            let tail = geometry
                .iter()
                .rev()
                .find(|placement| placement["id"] == id)
                .map(|placement| &placement["tail"])
                .filter(|tail| tail.is_object())
                .ok_or("dialogue tail is missing")?;
            expected.insert((panel_id, id), (item, tail));
        }
    }
    let mut observed = BTreeMap::new();
    for region in regions {
        if !keys(
            region,
            &[
                "panel_id",
                "text_id",
                "speaker",
                "voice_source",
                "speaker_anchor",
                "tip",
                "result",
            ],
        ) {
            return Err("region fields are invalid");
        }
        let panel_id = region["panel_id"]
            .as_str()
            .ok_or("region identity is invalid")?;
        let id = region["text_id"]
            .as_str()
            .ok_or("region identity is invalid")?;
        if !matches!(region["result"].as_str(), Some("pass" | "fail"))
            || observed.insert((panel_id, id), region).is_some()
        {
            return Err("region identity is invalid");
        }
    }
    if !observed.keys().eq(expected.keys()) {
        return Err("dialogue coverage is incomplete");
    }
    for (key, (item, tail)) in expected {
        let region = observed[&key];
        if ["speaker", "voice_source", "speaker_anchor"]
            .iter()
            .any(|field| !equal(&region[field], &item[field]))
            || !equal(&region["tip"], &tail["tip"])
        {
            return Err("region is stale");
        }
    }
    let failed = observed.values().any(|region| region["result"] == "fail");
    let warning = check["result"] == "warning" || check["severity"] == "warning";
    if (check["result"] == "pass" && failed)
        || (check["result"] == "fail" && !observed.is_empty() && !failed)
        || (warning && !failed)
    {
        return Err("check result is inconsistent");
    }
    Ok(())
}

fn equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(a), Value::Number(b)) if a.is_f64() || b.is_f64() => {
            a.as_f64() == b.as_f64()
        }
        (Value::Number(a), Value::Bool(b)) | (Value::Bool(b), Value::Number(a)) => {
            a.as_f64() == Some(u8::from(*b).into())
        }
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(l, r)| equal(l, r))
        }
        _ => left == right,
    }
}
