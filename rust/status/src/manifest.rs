use std::fs::File;
use std::io::{self, Read, Write};
use std::path::Path;

use serde::Serialize;
use serde_json::Value;
use serde_json::ser::{Formatter, PrettyFormatter, Serializer};

use crate::error::{Error, Result, contract};
use crate::project_fs;

use contract::{
    MAX_JSON_BYTES as MAX_BYTES, MAX_JSON_DEPTH as MAX_DEPTH, MAX_JSON_ENTRIES as MAX_ENTRIES,
    MAX_JSON_STRING_CHARS as MAX_STRING_CHARS,
};

pub fn read(dir: &File, name: &str) -> Result<Value> {
    read_document(dir, Path::new(name), name)
        .map(|(value, _)| value)
        .map_err(|error| {
            if error
                .detail()
                .is_some_and(|detail| detail.starts_with("JSONDecodeError:"))
            {
                Error::invalid("JSONDecodeError")
            } else {
                error
            }
        })
}

pub fn read_document(dir: &File, name_path: &Path, label: &str) -> Result<(Value, String)> {
    let _ = project_fs::metadata(dir, name_path)?;
    let file = project_fs::open_file(dir, name_path, false, false)?;
    let size_limit = || Error::limit(format!("the file size limit of {MAX_BYTES} bytes"));
    if file.metadata()?.len() > MAX_BYTES as u64 {
        return Err(size_limit());
    }
    let mut bytes = Vec::new();
    file.take(MAX_BYTES as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > MAX_BYTES {
        return Err(size_limit());
    }
    let text = String::from_utf8(bytes).map_err(|_| Error::invalid("UnicodeDecodeError"))?;
    let value = parse_document(&text, label)?;
    Ok((value, text))
}

pub fn parse_document(text: &str, label: &str) -> Result<Value> {
    check_structure(text, label)?;
    let value: Value = serde_json::from_str(text)
        .map_err(|error| Error::invalid(json_decode_detail(text, error)))?;
    let mut pending = vec![&value];
    while let Some(value) = pending.pop() {
        match value {
            Value::Object(fields) => pending.extend(fields.values()),
            Value::Array(items) => pending.extend(items),
            Value::String(text) if text.chars().count() > MAX_STRING_CHARS => {
                return Err(Error::limit(format!(
                    "the JSON string length limit of {MAX_STRING_CHARS} characters"
                )));
            }
            _ => {}
        }
    }
    Ok(value)
}

fn json_decode_detail(text: &str, error: serde_json::Error) -> String {
    let suffix = format!(" at line {} column {}", error.line(), error.column());
    let error_text = error.to_string();
    let cause = error_text.strip_suffix(&suffix).unwrap_or(&error_text);
    let preceding = text
        .split_inclusive('\n')
        .take(error.line().saturating_sub(1))
        .map(str::len)
        .sum::<usize>();
    let position = if error.is_eof() {
        text.len()
    } else {
        (preceding + error.column().saturating_sub(1)).min(text.len())
    };
    let position = text.floor_char_boundary(position);
    let prior = text[..position].trim_end();
    let message = match cause {
        "expected value" | "expected ident" | "invalid number" | "EOF while parsing a value" => {
            "Expecting value"
        }
        "key must be a string" => "Expecting property name enclosed in double quotes",
        "EOF while parsing an object" if prior.ends_with(['{', ',']) => {
            "Expecting property name enclosed in double quotes"
        }
        "expected `:`" => "Expecting ':' delimiter",
        "expected `,` or `}`"
        | "expected `,` or `]`"
        | "EOF while parsing an object"
        | "EOF while parsing a list" => "Expecting ',' delimiter",
        "trailing characters" => "Extra data",
        "trailing comma" if text[position..].starts_with('}') => {
            "Expecting property name enclosed in double quotes"
        }
        "trailing comma" => "Expecting value",
        _ => return format!("JSONDecodeError: {cause}{suffix}"),
    };
    let line = text[..position]
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count()
        + 1;
    let column = text[..position]
        .rsplit('\n')
        .next()
        .unwrap_or("")
        .chars()
        .count()
        + 1;
    let index = text[..position].chars().count();
    format!("JSONDecodeError: {message}: line {line} column {column} (char {index})")
}

fn check_structure(text: &str, name: &str) -> Result<()> {
    let mut commas = Vec::new();
    let mut quoted = false;
    let mut escaped = false;
    for byte in text.bytes() {
        if quoted {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
            continue;
        }
        match byte {
            b'"' => quoted = true,
            b'{' | b'[' => {
                if commas.len() == MAX_DEPTH {
                    return Err(Error::limit(format!(
                        "the JSON nesting depth limit of {MAX_DEPTH} for {name}"
                    )));
                }
                commas.push(0);
            }
            b'}' | b']' => {
                commas.pop();
            }
            b',' => {
                if let Some(count) = commas.last_mut() {
                    *count += 1;
                    if *count >= MAX_ENTRIES {
                        return Err(Error::limit(format!(
                            "the JSON collection size limit of {MAX_ENTRIES} entries"
                        )));
                    }
                }
            }
            _ => {}
        }
    }
    Ok(())
}

pub fn read_project(root: &File, normalize_legacy: bool) -> Result<Value> {
    let mut value = if normalize_legacy {
        read(root, "project.json")?
    } else {
        read_document(root, Path::new("project.json"), "project.json")
            .map_err(|error| {
                match error
                    .detail()
                    .and_then(|detail| detail.strip_prefix("JSONDecodeError: "))
                {
                    Some(detail) if !detail.contains(['/', '\\']) => Error::invalid(detail),
                    Some(_) => Error::invalid("JSONDecodeError"),
                    None => error,
                }
            })?
            .0
    };
    let manifest = value
        .as_object_mut()
        .ok_or_else(|| Error::invalid("expected a JSON object: <path>"))?;
    if let Some(version) = manifest.get("schema_version") {
        if !supported_version(version) {
            return Err(unsupported_version(version));
        }
    } else if normalize_legacy {
        manifest.insert(
            "schema_version".into(),
            contract::LEGACY_PROJECT_SCHEMA_VERSION.into(),
        );
    }
    Ok(value)
}

pub fn supported_version(version: &Value) -> bool {
    matches!(
        version.as_str(),
        Some(contract::CURRENT_PROJECT_SCHEMA_VERSION | contract::LEGACY_PROJECT_SCHEMA_VERSION)
    )
}

fn unsupported_version(version: &Value) -> Error {
    let detail = match version {
        Value::String(version) if version.contains(['/', '\\']) => {
            "UnsupportedSchemaVersionError".to_owned()
        }
        Value::String(version) => {
            let reason = match version_parts(version) {
                None => "invalid version format; expected major.minor".to_owned(),
                Some(parts)
                    if parts
                        > version_parts(contract::CURRENT_PROJECT_SCHEMA_VERSION)
                            .expect("valid project schema version") =>
                {
                    format!(
                        "reader supports up to {}; upgrade Comic Sol before opening this project",
                        contract::CURRENT_PROJECT_SCHEMA_VERSION
                    )
                }
                Some(_) => format!(
                    "reader requires at least {}; no migration path is registered",
                    contract::LEGACY_PROJECT_SCHEMA_VERSION
                ),
            };
            let version: String = version
                .chars()
                .flat_map(|ch| {
                    match ch {
                        '\n' => "\\n".into(),
                        '\r' => "\\r".into(),
                        '\t' => "\\t".into(),
                        ch if ch.is_control() => format!("\\x{:02x}", u32::from(ch)),
                        ch => ch.to_string(),
                    }
                    .chars()
                    .collect::<Vec<_>>()
                })
                .collect();
            format!("project schema {version} is unsupported: {reason}")
        }
        other => {
            let description = match other {
                Value::Null => "None".into(),
                Value::Bool(true) => "True".into(),
                Value::Bool(false) => "False".into(),
                Value::Number(number) => number.to_string(),
                _ => return Error::invalid("UnsupportedSchemaVersionError"),
            };
            format!(
                "project schema {description} is unsupported: schema version must be a string in major.minor format"
            )
        }
    };
    Error::invalid(detail)
}

fn version_parts(version: &str) -> Option<((usize, &str), (usize, &str))> {
    let (major, minor) = version.split_once('.')?;
    let digits = |part: &str| {
        !part.is_empty()
            && (part == "0" || !part.starts_with('0'))
            && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    (digits(major) && digits(minor)).then_some(((major.len(), major), (minor.len(), minor)))
}

struct StatusJson;

struct CompactJson;

struct CanonicalJson(PrettyFormatter<'static>);

impl Formatter for StatusJson {
    fn begin_array_value<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if !first {
            writer.write_all(b", ")?;
        }
        Ok(())
    }

    fn begin_object_key<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        self.begin_array_value(writer, first)
    }

    fn begin_object_value<W: Write + ?Sized>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }

    fn write_number_str<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        text: &str,
    ) -> io::Result<()> {
        if !text.contains(['.', 'e', 'E']) {
            return writer.write_all(if text == "-0" { b"0" } else { text.as_bytes() });
        }
        let value: f64 = text.parse().map_err(io::Error::other)?;
        if value.is_infinite() {
            let infinity = if value.is_sign_negative() {
                "-Infinity"
            } else {
                "Infinity"
            };
            return writer.write_all(infinity.as_bytes());
        }
        self.write_f64(writer, value)
    }

    fn write_f64<W: Write + ?Sized>(&mut self, writer: &mut W, value: f64) -> io::Result<()> {
        let number = format!("{value:?}");
        if let Some((significand, exponent)) = number.split_once('e') {
            let exponent: i32 = exponent.parse().map_err(io::Error::other)?;
            return write!(writer, "{significand}e{exponent:+03}");
        }
        writer.write_all(number.as_bytes())
    }
}

impl Formatter for CanonicalJson {
    fn begin_array<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.begin_array(w)
    }
    fn end_array<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.end_array(w)
    }
    fn begin_array_value<W: Write + ?Sized>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        self.0.begin_array_value(w, first)
    }
    fn end_array_value<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.end_array_value(w)
    }
    fn begin_object<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.begin_object(w)
    }
    fn end_object<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.end_object(w)
    }
    fn begin_object_key<W: Write + ?Sized>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        self.0.begin_object_key(w, first)
    }
    fn begin_object_value<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.begin_object_value(w)
    }
    fn end_object_value<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        self.0.end_object_value(w)
    }
    fn write_number_str<W: Write + ?Sized>(&mut self, w: &mut W, text: &str) -> io::Result<()> {
        StatusJson.write_number_str(w, text)
    }
    fn write_f64<W: Write + ?Sized>(&mut self, w: &mut W, value: f64) -> io::Result<()> {
        StatusJson.write_f64(w, value)
    }
}

impl Formatter for CompactJson {
    fn write_number_str<W: Write + ?Sized>(&mut self, w: &mut W, text: &str) -> io::Result<()> {
        StatusJson.write_number_str(w, text)
    }

    fn write_f64<W: Write + ?Sized>(&mut self, w: &mut W, value: f64) -> io::Result<()> {
        StatusJson.write_f64(w, value)
    }
}

pub fn compact(value: &Value) -> Vec<u8> {
    let mut bytes = Vec::new();
    value
        .serialize(&mut Serializer::with_formatter(&mut bytes, CompactJson))
        .expect("serialize JSON to memory");
    bytes
}

pub fn canonical(value: &Value) -> Vec<u8> {
    let mut bytes = Vec::new();
    value
        .serialize(&mut Serializer::with_formatter(
            &mut bytes,
            CanonicalJson(PrettyFormatter::new()),
        ))
        .expect("serialize JSON to memory");
    bytes.push(b'\n');
    bytes
}

pub fn write(value: &Value, mut writer: impl Write) -> io::Result<()> {
    value.serialize(&mut Serializer::with_formatter(&mut writer, StatusJson))?;
    writer.write_all(if cfg!(windows) { b"\r\n" } else { b"\n" })
}
