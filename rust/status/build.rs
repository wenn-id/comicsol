use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

fn embed_skill() {
    let root = Path::new("../../skills/comic-sol");
    let mut work = vec![root.to_owned()];
    let mut files = Vec::new();
    println!("cargo:rerun-if-changed={}", root.display());
    while let Some(directory) = work.pop() {
        for entry in fs::read_dir(directory).expect("canonical Comic Sol Skill") {
            let entry = entry.expect("Skill member");
            let name = entry.file_name();
            let name = name.to_str().expect("portable Skill member");
            if name == "__pycache__" || name.ends_with(".pyc") || name.ends_with(".pyo") {
                continue;
            }
            let path = entry.path();
            let kind = entry.file_type().expect("Skill member type");
            if kind.is_dir() {
                work.push(path);
            } else {
                assert!(
                    kind.is_file(),
                    "canonical Skill member must be a regular file"
                );
                let relative = path
                    .strip_prefix(root)
                    .expect("Skill member")
                    .components()
                    .map(|part| part.as_os_str().to_str().expect("Skill path"))
                    .collect::<Vec<_>>()
                    .join("/");
                assert!(
                    !relative.ends_with(".py"),
                    "Python source in Agent Skill bundle: {relative}"
                );
                files.push((
                    relative,
                    fs::canonicalize(path).expect("absolute Skill member"),
                ));
            }
        }
    }
    files.sort_unstable();
    let mut output = String::from("&[\n");
    for (relative, source) in files {
        output.push_str(&format!(
            "({relative:?}, include_bytes!({:?})),\n",
            source.to_str().expect("Skill source path")
        ));
    }
    output.push_str("]\n");
    fs::write(
        PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR")).join("skill.rs"),
        output,
    )
    .expect("write embedded Skill inventory");
}

fn build_raster() {
    println!("cargo:rerun-if-changed=native");
    println!("cargo:rerun-if-changed=../../requirements/locks/native-raster.cmake");
    for key in [
        "COMIC_SOL_CMAKE",
        "COMIC_SOL_CODEC_ARCHIVES",
        "CMAKE_GENERATOR",
        "CMAKE_MAKE_PROGRAM",
        "CMAKE_TOOLCHAIN_FILE",
    ] {
        println!("cargo:rerun-if-env-changed={key}");
    }
    let dir = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR")).join("native");
    let cmake = env::var_os("COMIC_SOL_CMAKE").unwrap_or_else(|| "cmake".into());
    let mut configure = Command::new(&cmake);
    configure
        .args(["-S", "native", "-B"])
        .arg(&dir)
        .arg("-DCMAKE_BUILD_TYPE=Release");
    for key in [
        "COMIC_SOL_CODEC_ARCHIVES",
        "CMAKE_MAKE_PROGRAM",
        "CMAKE_TOOLCHAIN_FILE",
    ] {
        if let Ok(value) = env::var(key) {
            configure.arg(format!("-D{key}={value}"));
        }
    }
    assert!(
        configure
            .status()
            .expect("run CMake for raster codecs")
            .success(),
        "configure native raster codecs"
    );
    assert!(
        Command::new(cmake)
            .arg("--build")
            .arg(&dir)
            .args(["--config", "Release", "--target", "comic_sol_raster"])
            .status()
            .expect("build native raster codecs with CMake")
            .success(),
        "build native raster codecs"
    );
    println!(
        "cargo:rustc-link-search=native={}",
        dir.join("codecs/lib").display()
    );
    let msvc = env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc");
    let linux = env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux");
    println!("cargo:rustc-link-lib=static=comic_sol_raster");
    if linux {
        println!("cargo:rustc-link-lib=static=harfbuzz");
        println!("cargo:rustc-link-lib=stdc++");
        println!("cargo:rustc-link-lib=dl");
    }
    for lib in ["freetype", if msvc { "jpeg-static" } else { "jpeg" }] {
        println!("cargo:rustc-link-lib=static={lib}");
    }
    if linux {
        // Bind to the same system zlib SONAME as the Linux Pillow wheel.
        println!("cargo:rustc-link-lib=dylib:+verbatim=libz.so.1");
    } else {
        println!(
            "cargo:rustc-link-lib=static={}",
            if msvc { "zlibstatic" } else { "z" }
        );
    }
}

fn assignment<'a>(source: &'a str, name: &str) -> &'a str {
    source
        .lines()
        .find_map(|line| line.trim().strip_prefix(name)?.strip_prefix(" = "))
        .unwrap_or_else(|| panic!("Missing canonical assignment: {name}"))
}

fn main() {
    build_raster();
    embed_skill();
    let version_file = "../../comic_sol_product/version.py";
    println!("cargo:rerun-if-changed={version_file}");
    let version = fs::read_to_string(version_file).expect("read canonical Comic Sol version");
    println!(
        "cargo:rustc-env=COMIC_SOL_VERSION={}",
        assignment(&version, "VERSION").trim_matches('"')
    );
    let schema = "../../scripts/schema.py";
    let limits = "../../scripts/input_limits.py";
    let project_io = "../../scripts/project_io.py";
    let errors = "../../comic_sol_product/errors.py";
    let font_coverage = "../../scripts/font_coverage.py";
    let stage_registry = "../../scripts/stage_registry.py";
    for path in [
        schema,
        limits,
        project_io,
        errors,
        font_coverage,
        stage_registry,
    ] {
        println!("cargo:rerun-if-changed={path}");
    }

    let schema = fs::read_to_string(schema).expect("read canonical project schema");
    let mut output = String::new();
    for name in [
        "LEGACY_PROJECT_SCHEMA_VERSION",
        "CURRENT_PROJECT_SCHEMA_VERSION",
    ] {
        let version = assignment(&schema, name).trim_matches('"');
        assert!(version.split_once('.').is_some(), "expected major.minor");
        output.push_str(&format!("pub const {name}: &str = {version:?};\n"));
    }
    assert!(schema.contains("{LEGACY_PROJECT_SCHEMA_VERSION, CURRENT_PROJECT_SCHEMA_VERSION}"));

    let limits = fs::read_to_string(limits).expect("read canonical JSON limits");
    for name in [
        "MAX_JSON_BYTES",
        "MAX_JSON_DEPTH",
        "MAX_JSON_ENTRIES",
        "MAX_JSON_STRING_CHARS",
    ] {
        let value: usize = assignment(&limits, name)
            .split('*')
            .map(|factor| factor.trim().parse::<usize>().expect("integer JSON limit"))
            .product();
        output.push_str(&format!("pub const {name}: usize = {value};\n"));
    }
    let project_io = fs::read_to_string(project_io).expect("read canonical transaction contract");
    let journal_version = assignment(&project_io, "JOURNAL_SCHEMA_VERSION").trim_matches('"');
    let lock_offset: u32 = assignment(&project_io, "_LOCK_BYTE_OFFSET")
        .parse()
        .expect("integer byte-range lock offset");
    output.push_str(&format!(
        "pub const JOURNAL_SCHEMA_VERSION: &str = {journal_version:?};\n"
    ));
    output.push_str(&format!(
        "#[cfg(windows)] pub const LOCK_BYTE_OFFSET: u32 = {lock_offset};\n"
    ));

    let errors = fs::read_to_string(errors).expect("read canonical CLI errors");
    for (name, code) in [
        ("USAGE", "CS-CLI-001"),
        ("INVALID", "CS-PROJ-001"),
        ("MISSING", "CS-PROJ-002"),
        ("PERMISSION", "CS-PROJ-003"),
        ("IO", "CS-PROJ-004"),
        ("UNEXPECTED", "CS-PROJ-005"),
        ("BOUNDARY", "CS-SEC-001"),
        ("LIMIT", "CS-SEC-002"),
        ("QUALITY", "CS-QA-001"),
        ("EXPORT", "CS-EXPORT-001"),
        ("TYPOGRAPHY", "CS-FONT-001"),
        ("HANDOFF", "CS-HANDOFF-001"),
        ("HANDOFF_ARCHIVE", "CS-HANDOFF-002"),
        ("HANDOFF_RESULT", "CS-HANDOFF-003"),
        ("MCP_REQUEST", "CS-MCP-001"),
        ("MCP_INTERNAL", "CS-MCP-002"),
        ("REPAIR", "CS-INSTALL-002"),
        ("ROLLBACK", "CS-INSTALL-003"),
    ] {
        let definition = errors
            .split("ErrorDefinition(")
            .find_map(|block| {
                let fields: Vec<_> = block
                    .lines()
                    .skip(1)
                    .take(5)
                    .filter_map(|line| line.trim().strip_prefix('"')?.strip_suffix("\","))
                    .collect();
                (fields.first() == Some(&code)).then_some(fields)
            })
            .expect("find canonical error definition");
        let fields: [&str; 5] = definition.try_into().expect("five error fields");
        output.push_str(&format!("pub const {name}: [&str; 5] = {fields:?};\n"));
    }
    let target = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo output directory"));
    let font = fs::read_to_string(font_coverage).expect("read font coverage policy");
    let blocks = font
        .split_once("UNICODE_BLOCKS:")
        .expect("Unicode block table")
        .1;
    let blocks = blocks
        .split_once("\n)")
        .expect("end of Unicode block table")
        .0;
    output.push_str("pub const UNICODE_BLOCKS: &[(u32, u32, &str, bool, bool, &str, &str)] = &[\n");
    for line in blocks.lines().skip(1).map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (function, fields) = line.split_once('(').expect("Unicode block constructor");
        let mut quoted = fields.split('"');
        let script = quoted.nth(1).expect("Unicode block script");
        let label = quoted.nth(1).expect("Unicode block label");
        let rest = quoted.next().expect("Unicode block range");
        let mut bounds = rest
            .split(',')
            .map(str::trim)
            .filter(|part| part.starts_with("0x"));
        let first = bounds.next().expect("start of Unicode block");
        let last = bounds
            .next()
            .map(|part| part.trim_end_matches(')'))
            .expect("end of Unicode block");
        let linear = match function {
            "_linear" | "_marks" => true,
            "_complex" => false,
            _ => panic!("unknown Unicode block constructor {function}"),
        };
        let combining = function == "_marks";
        let reason = if linear {
            ""
        } else {
            let symbol = rest
                .rsplit(',')
                .find_map(|part| {
                    let part = part.trim().trim_end_matches(')');
                    part.starts_with('_').then_some(part)
                })
                .expect("complex block reason");
            assignment(&font, symbol).trim_matches('"')
        };
        output.push_str(&format!(
            "({first}, {last}, {script:?}, {linear}, {combining}, {reason:?}, {label:?}),\n"
        ));
    }
    output.push_str("];\n");
    let recommendations = font
        .split_once("SCRIPT_FONTS:")
        .expect("recommended script fonts")
        .1
        .split_once("\n)")
        .expect("end of script fonts")
        .0;
    output.push_str(
        "pub const SCRIPT_FONT_RECOMMENDATIONS: &[(&str, &str, &str, &str, &str)] = &[\n",
    );
    for block in recommendations.split("ScriptFont(").skip(1) {
        let data: Vec<_> = block.split('"').skip(1).step_by(2).collect();
        assert_eq!(data.len(), 5, "script font record");
        output.push_str(&format!(
            "({:?}, {:?}, {:?}, {:?}, {:?}),\n",
            data[0], data[1], data[4], data[2], data[3]
        ));
    }
    output.push_str("];\n");
    let stages = fs::read_to_string(stage_registry).expect("read canonical stage registry");
    let registry = stages
        .split_once("STAGE_REGISTRY:")
        .expect("stage registry")
        .1
        .split_once("\n)")
        .expect("end of stage registry")
        .0;
    let mut resume = String::from("const STAGES: &[Definition] = &[\n");
    for block in registry.split("StageDefinition(").skip(1) {
        let field = |name| {
            block
                .lines()
                .find_map(|line| line.trim().strip_prefix(name)?.strip_prefix('='))
                .unwrap_or_else(|| panic!("stage registry field: {name}"))
        };
        let runner = field("runner").trim_end_matches(',').trim_matches('"');
        let runner = match field("next_action").trim_end_matches(',').trim_matches('"') {
            "agent" => String::from("None"),
            "command" => format!("Some({runner:?})"),
            other => panic!("unknown stage action {other}"),
        };
        let artifacts = block
            .lines()
            .find_map(|line| line.trim().strip_prefix("artifacts=(")?.strip_suffix("),"))
            .unwrap_or_default();
        resume.push_str(&format!(
            "Definition {{ name: {}, rewind: {}, complete: {}, runner: {runner}, artifacts: &[{artifacts}] }},\n",
            field("name").trim_end_matches(','),
            field("invalidation_status").trim_end_matches(','),
            field("completion_status").trim_end_matches(','),
        ));
        assert_eq!(field("name"), field("material_kind"));
        assert_eq!(field("name"), field("output_kind"));
    }
    resume.push_str("];\n");
    fs::write(target.join("resume_stages.rs"), resume).expect("write generated stages");
    fs::write(target.join("contract.rs"), output).expect("write generated contract");
}
