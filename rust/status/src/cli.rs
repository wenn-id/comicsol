use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::sync::LazyLock;

use serde::Deserialize;
use serde_json::Value;

use crate::error::{Error, Result};

#[derive(Deserialize)]
struct Parser {
    help: String,
    flags: Vec<Flag>,
    #[serde(default)]
    positionals: Vec<String>,
    subcommand: Option<String>,
    #[serde(default)]
    children: BTreeMap<String, Parser>,
    #[serde(default)]
    names: Vec<String>,
}

#[derive(Deserialize)]
struct Flag {
    name: String,
    aliases: Vec<String>,
    takes_value: bool,
    #[serde(default)]
    required: bool,
    #[serde(default)]
    repeat: bool,
    #[serde(default)]
    choices: Vec<Value>,
    #[serde(rename = "type")]
    kind: Option<String>,
}

static CLI: LazyLock<Parser> = LazyLock::new(|| {
    let mut parser: Parser =
        serde_json::from_str(include_str!("cli-contract.json")).expect("installed CLI contract");
    let skill = skill_parser();
    parser
        .help
        .push_str("\nUse comic-sol skill --help for Agent Skill workflow commands.\n");
    parser.names.push("skill".into());
    parser.children.insert("skill".into(), skill);
    parser
});

fn skill_parser() -> Parser {
    type Command = (
        &'static str,
        Vec<&'static str>,
        Vec<(&'static str, &'static str)>,
    );
    let commands: Vec<Command> = serde_json::from_str(include_str!("skill-contract.json"))
        .expect("Agent Skill CLI contract");
    let mut root = Parser {
        help: "usage: comic-sol [--json] skill COMMAND [OPTIONS]\n\nCommands:\n".into(),
        flags: vec![help_flag()],
        positionals: Vec::new(),
        subcommand: Some("command".into()),
        names: Vec::new(),
        children: BTreeMap::new(),
    };
    for (name, positionals, options) in commands {
        let mut usage = format!("usage: comic-sol [--json] skill {name}");
        for positional in &positionals {
            usage.push_str(&format!(" {}", positional.to_ascii_uppercase()));
        }
        let mut flags = vec![help_flag()];
        for (option, kind) in options {
            let key = option.strip_prefix("--").expect("long skill option");
            usage.push_str(&format!(
                " [{option}{}{}]",
                if kind.is_empty() { "" } else { " " },
                if kind.is_empty() {
                    String::new()
                } else {
                    key.to_ascii_uppercase()
                },
            ));
            flags.push(Flag {
                name: key.into(),
                aliases: vec![option.into()],
                takes_value: !kind.is_empty(),
                required: false,
                repeat: kind == "repeat",
                choices: Vec::new(),
                kind: (kind == "int").then(|| "int".into()),
            });
        }
        root.help.push_str(&format!("  {name}\n"));
        root.names.push(name.into());
        root.children.insert(
            name.into(),
            Parser {
                help: format!("{usage}\n"),
                flags,
                positionals: positionals.iter().map(|name| (*name).into()).collect(),
                subcommand: None,
                children: BTreeMap::new(),
                names: Vec::new(),
            },
        );
    }
    root
}

fn help_flag() -> Flag {
    Flag {
        name: "help".into(),
        aliases: vec!["-h".into(), "--help".into()],
        takes_value: false,
        required: false,
        repeat: false,
        choices: Vec::new(),
        kind: None,
    }
}

pub enum Invocation {
    Help(&'static str),
    Version,
    Run(Arguments),
}

#[derive(Default)]
pub struct Arguments {
    pub command: String,
    values: BTreeMap<&'static str, Vec<OsString>>,
}

impl Arguments {
    fn insert(&mut self, key: &'static str, value: OsString, repeat: bool) {
        let values = self.values.entry(key).or_default();
        if !repeat {
            values.clear();
        }
        values.push(value);
    }

    pub fn has(&self, key: &str) -> bool {
        self.values.contains_key(key)
    }

    pub fn text(&self, key: &str) -> Option<&str> {
        self.values.get(key)?.last()?.to_str()
    }

    pub fn values(&self, key: &str) -> impl Iterator<Item = &OsStr> {
        self.values
            .get(key)
            .into_iter()
            .flatten()
            .map(OsString::as_os_str)
    }

    pub fn path(&self, key: &str) -> Option<&Path> {
        self.values.get(key)?.last().map(Path::new)
    }

    pub fn paths(&self, key: &str) -> &Path {
        self.path(key).expect("required positional or option")
    }
}

pub fn parse(args: &[OsString]) -> Result<Invocation> {
    let mut parser: &'static Parser = &CLI;
    let mut command = Arguments::default();
    let mut position = 0;
    let mut options = true;
    let mut extra = Vec::new();
    let mut args = args.iter().peekable();
    while let Some(arg) = args.next() {
        if options && arg == "--" && parser.subcommand.is_none() {
            options = false;
            continue;
        }
        if options && let Some(text) = arg.to_str().filter(|_| looks_like_option(arg)) {
            let (word, attached) = text
                .split_once('=')
                .map_or((text, None), |(a, b)| (a, Some(b)));
            if let Some(flag) = resolve_flag(parser, word)? {
                let value = if flag.takes_value {
                    attached
                        .map(OsString::from)
                        .or_else(|| args.next_if(|arg| !looks_like_option(arg)).cloned())
                        .ok_or_else(|| {
                            Error::usage(format!(
                                "argument {}: expected one argument",
                                flag.aliases.join("/")
                            ))
                        })?
                } else if let Some(value) = attached {
                    return Err(Error::usage(format!(
                        "argument {}: ignored explicit argument '{}'",
                        flag.aliases.join("/"),
                        value
                    )));
                } else {
                    OsString::new()
                };
                match flag.name.as_str() {
                    "help" => return Ok(Invocation::Help(&parser.help)),
                    "version" => return Ok(Invocation::Version),
                    _ => {}
                }
                let value = validate_value(flag, value)?;
                command.insert(&flag.name, value, flag.repeat);
                continue;
            } else if text != "--" {
                extra.push(arg.to_string_lossy());
                continue;
            }
        }
        if let Some(name) = &parser.subcommand {
            let text = arg.to_string_lossy();
            parser = parser.children.get(text.as_ref()).ok_or_else(|| {
                let choices = parser.names.join(", ");
                Error::usage(format!(
                    "argument {name}: invalid choice: '{text}' (choose from {choices})"
                ))
            })?;
            if !command.command.is_empty() {
                command.command.push('.');
            }
            command.command.push_str(&text);
            continue;
        }
        if let Some(name) = parser.positionals.get(position) {
            command.insert(name, arg.clone(), false);
            position += 1;
        } else {
            extra.push(arg.to_string_lossy());
        }
    }
    let mut missing: Vec<_> = parser.positionals[position..]
        .iter()
        .map(String::as_str)
        .collect();
    missing.extend(parser.subcommand.as_deref());
    missing.extend(
        parser
            .flags
            .iter()
            .filter(|flag| flag.required && !command.has(&flag.name))
            .map(|flag| flag.aliases[0].as_str()),
    );
    if !missing.is_empty() {
        return Err(Error::usage(format!(
            "the following arguments are required: {}",
            missing.join(", ")
        )));
    }
    if !extra.is_empty() {
        return Err(Error::usage(format!(
            "unrecognized arguments: {}",
            extra.join(" ")
        )));
    }
    validate_init(&command)?;
    Ok(Invocation::Run(command))
}

fn looks_like_option(arg: &OsStr) -> bool {
    let text = arg.to_string_lossy();
    text.starts_with('-') && text != "-" && text.parse::<f64>().is_err()
}

fn resolve_flag<'a>(parser: &'a Parser, word: &str) -> Result<Option<&'a Flag>> {
    if let Some(flag) = parser
        .flags
        .iter()
        .find(|flag| flag.aliases.iter().any(|a| a == word))
    {
        return Ok(Some(flag));
    }
    if !word.starts_with("--") || word.len() < 3 {
        return Ok(None);
    }
    let matches: Vec<_> = parser
        .flags
        .iter()
        .flat_map(|flag| {
            flag.aliases
                .iter()
                .filter(move |name| name.starts_with(word))
                .map(move |name| (name, flag))
        })
        .collect();
    match matches.as_slice() {
        [] => Ok(None),
        [(_, flag)] => Ok(Some(flag)),
        _ => Err(Error::usage(format!(
            "ambiguous option: {word} could match {}",
            matches
                .iter()
                .map(|(name, _)| name.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ))),
    }
}

fn validate_value(flag: &Flag, value: OsString) -> Result<OsString> {
    let text = value.to_string_lossy();
    let key = flag.aliases.join("/");
    let kind = flag.kind.as_deref();
    let integer = matches!(
        kind,
        Some("int" | "dogfood_minutes" | "dogfood_failed_attempts")
    );
    let canonical = integer.then(|| decimal(&text)).flatten();
    let parse_int = || {
        canonical
            .as_deref()
            .and_then(|number| number.parse::<i64>().ok())
    };
    let contract_value = match kind {
        Some("int") => {
            if canonical.is_none() {
                return Err(Error::usage(format!(
                    "argument {key}: invalid int value: '{text}'"
                )));
            }
            if flag.choices.is_empty() {
                return Ok(canonical.expect("validated decimal").into());
            }
            parse_int().map_or(Value::Null, Value::from)
        }
        Some("dogfood_minutes") => bounded_int(&key, parse_int(), 10080)?,
        Some("dogfood_failed_attempts") => bounded_int(&key, parse_int(), 1000)?,
        Some("dogfood_yes_no") if !matches!(text.as_ref(), "yes" | "no") => {
            return Err(Error::usage(format!("argument {key}: must be yes or no")));
        }
        _ => Value::String(text.to_string()),
    };
    if !flag.choices.is_empty() && !flag.choices.contains(&contract_value) {
        let allowed = flag
            .choices
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map_or_else(|| value.to_string(), str::to_owned)
            })
            .collect::<Vec<_>>()
            .join(", ");
        return Err(Error::usage(format!(
            "argument {key}: invalid choice: '{text}' (choose from {allowed})"
        )));
    }
    Ok(if integer {
        contract_value.to_string().into()
    } else {
        value
    })
}

fn bounded_int(key: &str, parsed: Option<i64>, max: i64) -> Result<Value> {
    match parsed {
        Some(value) if (0..=max).contains(&value) => Ok(Value::from(value)),
        _ => Err(Error::usage(format!(
            "argument {key}: must be an integer from 0 through {max}"
        ))),
    }
}

fn decimal(text: &str) -> Option<String> {
    let mut text = text
        .trim_matches(crate::validation::python_whitespace)
        .chars()
        .peekable();
    let mut digits = String::new();
    if text.next_if_eq(&'-').is_some() {
        digits.push('-');
    } else {
        text.next_if_eq(&'+');
    }
    for count in 0..=4300 {
        let Some(digit) = text.next() else {
            return (count > 0).then_some(digits);
        };
        if count == 4300 {
            return None;
        }
        let digit = if digit.is_ascii_digit() {
            digit
        } else {
            let decimal =
                |value: char| crate::validation::is_decimal(value.encode_utf8(&mut [0; 4]));
            if !decimal(digit) {
                return None;
            }
            let value = u32::from(digit);
            let offset = (1..)
                .take_while(|offset| char::from_u32(value - offset).is_some_and(decimal))
                .count()
                % 10;
            char::from(b'0' + offset as u8)
        };
        digits.push(digit);
        if text.next_if_eq(&'_').is_some() && text.peek().is_none() {
            return None;
        }
    }
    None
}

fn validate_init(command: &Arguments) -> Result<()> {
    if command.command != "init" {
        return Ok(());
    }
    let source_flags = ["source", "request_json", "page_count"];
    let has_source = source_flags.iter().any(|flag| command.has(flag));
    let starter = command.has("starter");
    let title = command.has("title");
    if command.has("interactive") {
        if command.has("as_json") {
            return Err(Error::usage(
                "--interactive cannot be combined with --json".into(),
            ));
        }
        if has_source
            || starter
            || title
            || [
                "output_root",
                "image_capability_status",
                "image_capability_name",
                "supports_reference_images",
                "supports_dimensions",
            ]
            .iter()
            .any(|flag| command.has(flag))
        {
            return Err(Error::usage(
                "--interactive cannot be combined with init data arguments".into(),
            ));
        }
    } else if starter && has_source {
        return Err(Error::usage(
            "--starter cannot be combined with --source, --request-json, or --page-count".into(),
        ));
    } else {
        let missing = [
            (!title, "--title"),
            (!starter && !command.has("source"), "--source"),
        ]
        .into_iter()
        .filter_map(|(missing, name)| missing.then_some(name))
        .collect::<Vec<_>>();
        if !missing.is_empty() {
            return Err(Error::usage(format!(
                "the following arguments are required: {}",
                missing.join(", ")
            )));
        }
    }
    Ok(())
}
