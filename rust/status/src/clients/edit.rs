use std::ops::Range;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::manifest;

const SERVERS: &str = "mcpServers";
const TOML_SERVERS: &str = "mcp_servers";
const SERVER: &str = "comic-sol";
static SECTION: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
    r#"^[ \t]*\[\s*(?:mcp_servers|"mcp_servers"|'mcp_servers')\s*\.\s*(?:comic-sol|"comic-sol"|'comic-sol')\s*\][ \t]*(?:#[^\n]*)?\r?\n?$"#,
).expect("Codex server table")
});
static ANY_SECTION: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[ \t]*\[{1,2}[^\r\n]+\]{1,2}[ \t]*(?:#[^\n]*)?\r?\n?$").expect("TOML table")
});

pub fn update(raw: &[u8], codex: bool, entry: Option<&Value>) -> Result<Option<Vec<u8>>, String> {
    let text = std::str::from_utf8(raw).map_err(|_| "config is not valid UTF-8".to_owned())?;
    let candidate = if codex {
        edit_toml(text, entry)?
    } else {
        edit_json(text, entry)?
    };
    Ok(candidate.map(String::into_bytes))
}

fn json_config(text: &str) -> Result<Value, String> {
    let value = manifest::parse_document(text, "client config").map_err(|error| {
        error
            .detail()
            .unwrap_or("client config is not valid JSON")
            .trim_start_matches("JSONDecodeError: ")
            .to_owned()
    })?;
    if !value.is_object() {
        return Err("client config root must be an object".into());
    }
    if value
        .get(SERVERS)
        .is_some_and(|servers| !servers.is_object())
    {
        return Err(format!("{SERVERS} must be an object"));
    }
    Ok(value)
}

fn edit_json(text: &str, entry: Option<&Value>) -> Result<Option<String>, String> {
    let original = json_config(text)?;
    let existing = original[SERVERS].get(SERVER);
    if existing == entry {
        return Ok(None);
    }
    let start = text.len() - text.trim_start().len();
    let end = text.trim_end().len() - 1;
    let root = members(text, start + 1..end)?;
    let mapping = unique(&root, SERVERS)?;
    let new = if let Some(mapping) = mapping {
        let range = mapping.value.start + 1..mapping.value.end - 1;
        let servers = members(text, range.clone())?;
        let current = unique(&servers, SERVER)?;
        if let Some(entry) = entry {
            let entry = pretty(entry);
            if let Some(owned) = current {
                if !existing.is_some_and(Value::is_object) {
                    return Err("comic-sol entry must be an object".into());
                }
                let indent = indentation(&text[line_start(text, owned.key.start)..]);
                splice(
                    text,
                    owned.value.clone(),
                    &entry.replace('\n', &format!("\n{indent}")),
                )
            } else {
                let body = &text[range.clone()];
                let nonempty = body.trim_end();
                let indent = if nonempty.is_empty() {
                    format!(
                        "{}  ",
                        indentation(&text[line_start(text, mapping.key.start)..])
                    )
                } else {
                    indentation(
                        nonempty
                            .lines()
                            .find(|line| !line.trim().is_empty())
                            .unwrap_or_default(),
                    )
                    .to_owned()
                };
                let member = format!("\"{SERVER}\": {entry}").replace('\n', &format!("\n{indent}"));
                let insert = if nonempty.is_empty() {
                    range.start
                } else {
                    range.end
                };
                let separator = if nonempty.is_empty() { "\n" } else { ",\n" };
                splice(
                    text,
                    insert..insert,
                    &format!("{separator}{indent}{member}"),
                )
            }
        } else {
            let position = servers
                .iter()
                .position(|member| member.name == SERVER)
                .ok_or("comic-sol entry could not be located")?;
            let owned = &servers[position];
            let remove = if servers.len() == 1 {
                range.start..owned.value.end
            } else if position == 0 {
                let separator = text[owned.value.end..servers[1].key.start]
                    .find(',')
                    .ok_or("comic-sol entry separator could not be located")?;
                range.start..owned.value.end + separator + 1
            } else {
                servers[position - 1].value.end..owned.value.end
            };
            splice(text, remove, "")
        }
    } else if let Some(entry) = entry {
        let body = format!("\"{SERVER}\": {}", pretty(entry)).replace('\n', "\n    ");
        let (position, separator) = if root.is_empty() {
            (start + 1, "\n")
        } else {
            (text[..end].trim_end().len(), ",\n")
        };
        splice(
            text,
            position..position,
            &format!("{separator}  \"{SERVERS}\": {{\n    {body}\n  }}"),
        )
    } else {
        return Ok(None);
    };
    let mut expected = original;
    if let Some(entry) = entry {
        if expected.get(SERVERS).is_none() {
            expected[SERVERS] = serde_json::json!({});
        }
        expected[SERVERS][SERVER] = entry.clone();
    } else {
        expected[SERVERS]
            .as_object_mut()
            .expect("existing mappings")
            .remove(SERVER);
    }
    if json_config(&new)? != expected {
        return Err(
            "client config edit would change settings outside the Comic Sol integration".into(),
        );
    }
    Ok(Some(new))
}

struct Member {
    name: String,
    key: Range<usize>,
    value: Range<usize>,
}

fn members(text: &str, range: Range<usize>) -> Result<Vec<Member>, String> {
    let mut cursor = range.start;
    let mut result = Vec::new();
    while cursor < range.end {
        while cursor < range.end && b" \n\r\t,".contains(&text.as_bytes()[cursor]) {
            cursor += 1;
        }
        if cursor == range.end {
            break;
        }
        let (key, key_end) = raw_value(text, cursor)?;
        let sep = text[key_end..range.end]
            .find(':')
            .ok_or("client config member separator could not be located")?
            + key_end
            + 1;
        let value_start = text.len()
            - text[sep..]
                .trim_start_matches([' ', '\r', '\n', '\t'])
                .len();
        let (_, value_end) = raw_value(text, value_start)?;
        result.push(Member {
            name: key
                .as_str()
                .ok_or("client config member key must be a string")?
                .to_owned(),
            key: cursor..key_end,
            value: value_start..value_end,
        });
        cursor = value_end;
    }
    Ok(result)
}

fn raw_value(text: &str, start: usize) -> Result<(Value, usize), String> {
    let mut parser = serde_json::Deserializer::from_str(&text[start..]).into_iter::<Value>();
    let value = parser
        .next()
        .and_then(std::result::Result::ok)
        .ok_or("client config value could not be decoded")?;
    Ok((value, start + parser.byte_offset()))
}

fn unique<'a>(items: &'a [Member], name: &str) -> Result<Option<&'a Member>, String> {
    let mut matches = items.iter().filter(|member| member.name == name);
    let item = matches.next();
    if matches.next().is_some() {
        return Err(format!("{name} entry is ambiguous"));
    }
    Ok(item)
}

fn line_start(text: &str, position: usize) -> usize {
    text[..position].rfind('\n').map_or(0, |index| index + 1)
}

fn indentation(text: &str) -> &str {
    &text[..text.len() - text.trim_start().len()]
}

fn splice(text: &str, range: Range<usize>, replacement: &str) -> String {
    format!(
        "{}{}{}",
        &text[..range.start],
        replacement,
        &text[range.end..]
    )
}

fn pretty(value: &Value) -> String {
    String::from_utf8(manifest::canonical(value))
        .expect("UTF-8 JSON")
        .trim_end()
        .to_owned()
}

fn edit_toml(text: &str, entry: Option<&Value>) -> Result<Option<String>, String> {
    let before = toml_config(text)?;
    let existing = before
        .get(TOML_SERVERS)
        .and_then(toml::Value::as_table)
        .and_then(|servers| servers.get(SERVER));
    if entry.is_none() && existing.is_none() {
        return Ok(None);
    }
    let replacement = entry.map_or_else(String::new, |entry| {
        let command = entry["command"].to_string();
        let args = entry["args"]
            .as_array()
            .expect("integration args")
            .iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        format!("[{TOML_SERVERS}.{SERVER}]\ncommand = {command}\nargs = [{args}]\n")
    });
    let mut state = None;
    let mut offset = 0;
    let mut location = None;
    let mut next = None;
    for line in text.split_inclusive('\n') {
        if state.is_none() && location.is_some() && ANY_SECTION.is_match(line) {
            next = Some(offset);
            break;
        }
        if state.is_none() && SECTION.is_match(line) {
            location = Some(offset);
        }
        scan_line(line.as_bytes(), &mut state);
        offset += line.len();
    }
    let new = if let Some(start) = location {
        let boundary = next.unwrap_or(text.len());
        let end = table_end(text, start..boundary);
        if !replacement.is_empty() && text[start..end].trim() == replacement.trim() {
            return Ok(None);
        }
        let spacing = if !replacement.is_empty() && next == Some(end) {
            "\n"
        } else {
            ""
        };
        splice(text, start..end, &format!("{replacement}{spacing}"))
    } else {
        if existing.is_some() {
            return Err(
                "Codex config edit would change settings outside mcp_servers.comic-sol".into(),
            );
        }
        let separator = if text.is_empty() || text.ends_with("\n\n") {
            ""
        } else if text.ends_with('\n') {
            "\n"
        } else {
            "\n\n"
        };
        format!("{text}{separator}{replacement}")
    };
    let after = toml_config(&new)?;
    let filtered = |mut config: toml::Table| {
        let mut servers = config
            .remove(TOML_SERVERS)
            .and_then(|value| value.as_table().cloned())
            .unwrap_or_default();
        let owned = servers.remove(SERVER);
        (config, servers, owned)
    };
    let (old, old_servers, _) = filtered(before);
    let (new_fields, new_servers, owned) = filtered(after);
    let expected = entry.map(|entry| {
        let value = toml::Value::try_from(entry).expect("representable integration entry");
        value
    });
    if old != new_fields || old_servers != new_servers || owned != expected {
        return Err("Codex config edit would change settings outside mcp_servers.comic-sol".into());
    }
    Ok(Some(new))
}

fn toml_config(text: &str) -> Result<toml::Table, String> {
    let fields = text
        .parse::<toml::Table>()
        .map_err(|_| "Codex config is not valid TOML".to_owned())?;
    if fields
        .get(TOML_SERVERS)
        .is_some_and(|value| !value.is_table())
    {
        return Err("mcp_servers must be a TOML table".into());
    }
    Ok(fields)
}

fn table_end(text: &str, range: Range<usize>) -> usize {
    let mut state = None;
    let mut offset = range.start;
    let mut end = offset;
    for line in text[range].split_inclusive('\n') {
        offset += line.len();
        if state.is_some() || (!line.trim().is_empty() && !line.trim_start().starts_with('#')) {
            end = offset;
        }
        scan_line(line.as_bytes(), &mut state);
    }
    end
}

fn scan_line(bytes: &[u8], state: &mut Option<(u8, usize)>) {
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if let Some((quote, count)) = *state {
            if quote == b'"' && byte == b'\\' {
                index += 2;
                continue;
            }
            if bytes[index..]
                .iter()
                .take(count)
                .filter(|&&ch| ch == quote)
                .count()
                == count
            {
                *state = None;
                index += count;
                continue;
            }
        } else if byte == b'#' {
            break;
        } else if matches!(byte, b'\'' | b'"') {
            let count = if bytes[index..]
                .get(..3)
                .is_some_and(|triple| triple == [byte; 3])
            {
                3
            } else {
                1
            };
            *state = Some((byte, count));
            index += count;
            continue;
        }
        index += 1;
    }
}
