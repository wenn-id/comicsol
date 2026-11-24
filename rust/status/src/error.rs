use std::io;

use serde_json::{Value, json};

pub mod contract {
    include!(concat!(env!("OUT_DIR"), "/contract.rs"));
}

pub type Result<T> = std::result::Result<T, Error>;

#[cfg_attr(test, derive(Debug))]
pub struct Error {
    definition: [&'static str; 5],
    detail: Option<String>,
    exception: &'static str,
    pub exit: u8,
    pub usage: bool,
}

impl Error {
    pub fn invalid(detail: impl Into<String>) -> Self {
        Self::new(contract::INVALID, 2, Some(detail.into()))
    }

    pub fn boundary(detail: &str) -> Self {
        Self::new(contract::BOUNDARY, 2, Some(detail.into()))
    }

    pub fn limit(detail: impl Into<String>) -> Self {
        Self::new(
            contract::LIMIT,
            2,
            Some(format!("security-error: input exceeds {}", detail.into())),
        )
    }

    pub fn usage(detail: String) -> Self {
        Self {
            usage: true,
            ..Self::new(contract::USAGE, 2, Some(detail))
        }
    }

    fn new(definition: [&'static str; 5], exit: u8, detail: Option<String>) -> Self {
        Self {
            definition,
            exit,
            detail,
            exception: "ValueError",
            usage: false,
        }
    }

    pub fn validation_failure(issues: Value) -> Value {
        let mut payload = Self::new(contract::QUALITY, 2, None).payload("validate");
        payload["data"] = issues;
        payload
    }

    pub fn quality(detail: impl Into<String>) -> Self {
        Self::new(contract::QUALITY, 2, Some(detail.into()))
    }

    pub fn export(detail: impl Into<String>) -> Self {
        Self::new(contract::EXPORT, 2, Some(detail.into()))
    }

    pub fn typography(detail: impl Into<String>) -> Self {
        Self::new(contract::TYPOGRAPHY, 2, Some(detail.into()))
    }

    pub fn handoff(detail: String) -> Self {
        Self::new(contract::HANDOFF, 2, Some(detail))
    }

    pub fn handoff_result(detail: String) -> Self {
        Self {
            exception: "HandoffResultError",
            ..Self::new(contract::HANDOFF_RESULT, 2, Some(detail))
        }
    }

    pub fn archive(detail: impl Into<String>) -> Self {
        Self {
            exception: "HandoffArchiveError",
            ..Self::new(contract::HANDOFF_ARCHIVE, 2, Some(detail.into()))
        }
    }

    pub fn skill_verification(detail: &str) -> Self {
        Self {
            exception: "SkillVerificationError",
            ..Self::new(contract::UNEXPECTED, 1, Some(detail.into()))
        }
    }

    pub fn mcp_request(detail: &str) -> Self {
        Self::new(contract::MCP_REQUEST, 2, Some(detail.into()))
    }

    pub fn as_mcp_request(self) -> Self {
        let details = [
            "title must be a non-empty string of at most 200 characters",
            "source must be at most 200 KiB as UTF-8 bytes",
            "request_settings must be a JSON object",
            "sensitive request setting is not allowed",
            "request setting keys must be strings",
            "unsupported request setting",
            "request mode must be one of short_prompt, pasted_story, source_file, or resume",
            "request language must be a non-empty language tag",
            "request title must be a non-empty string of at most 200 characters",
            "page count must be an integer from 1 to 4",
            "narrative field must not contain secrets or credentials",
        ];
        if let Some(prefix) = self
            .detail()
            .and_then(|detail| details.iter().find(|&&prefix| detail.starts_with(prefix)))
        {
            Self::mcp_request(prefix)
        } else {
            self
        }
    }

    pub fn mcp_payload(self) -> Value {
        let definition = if self.definition == contract::UNEXPECTED {
            contract::MCP_INTERNAL
        } else {
            self.definition
        };
        let [code, category, message, reason, recovery] = definition;
        let raw = self.detail().unwrap_or_default();
        let legacy = if definition == contract::BOUNDARY && raw.starts_with("security-error") {
            raw.to_owned()
        } else if definition == contract::MCP_REQUEST {
            format!("invalid-request: {raw}")
        } else {
            match self.exception {
                "FileNotFoundError" => "not-found: required project data was not found",
                "PermissionError" => "permission-denied: project data could not be accessed",
                "OSError" | "IsADirectoryError" | "NotADirectoryError" | "FileExistsError" => {
                    "io-error: project data operation failed"
                }
                "ValueError" | "HandoffResultError" => {
                    "invalid-data: tool request or project data is invalid"
                }
                _ => "internal-error: tool operation failed",
            }
            .to_owned()
        };
        json!({"code": code, "category": category, "message": message, "reason": reason,
            "recovery": recovery, "command": null, "legacy_message": legacy})
    }

    pub fn detail(&self) -> Option<&str> {
        self.detail.as_deref()
    }

    pub fn code(&self) -> &str {
        self.definition[0]
    }

    pub fn exception(&self) -> &'static str {
        self.exception
    }

    pub fn is_exists(&self) -> bool {
        self.exception == "FileExistsError"
    }

    pub fn human(&self) -> String {
        let [code, category, message, reason, recovery] = self.definition;
        let detail = self.detail.as_deref().unwrap_or(self.exception);
        let detail = if detail.contains(['/', '\\']) {
            if self.usage {
                "CliUsageError"
            } else {
                self.exception
            }
        } else {
            detail
        };
        let detail = crate::human::escape(detail);
        format!(
            "ERROR {code} [{category}]: {message}\nDetail: {detail}\nReason: {reason}\nRecovery: {recovery}"
        )
    }

    pub fn validation_human(count: usize) -> String {
        Self::quality(format!("project validation reported {count} issue(s)")).human()
    }

    pub fn from_path_io(error: io::Error) -> Self {
        let missing = error.kind() == io::ErrorKind::NotFound;
        let mut public = Self::from(error);
        if missing {
            public.detail = Some("[Errno 2] No such file or directory: '<path>'".into());
        }
        public
    }

    pub fn dogfood(detail: impl Into<String>) -> Self {
        let detail = detail.into();
        let definition = if detail.starts_with("security-error") {
            contract::BOUNDARY
        } else {
            contract::INVALID
        };
        let detail = if detail.contains(['/', '\\']) {
            "DogfoodReportError".to_owned()
        } else {
            detail
        };
        Self {
            exception: "DogfoodReportError",
            ..Self::new(definition, 2, Some(detail))
        }
    }

    pub fn payload(self, command: &str) -> Value {
        let command = (!command.is_empty()).then_some(command);
        let [code, category, message, reason, recovery] = self.definition;
        let mut error = json!({
            "code": code, "category": category, "message": message,
            "reason": reason, "recovery": recovery, "command": command,
        });
        if let Some(detail) = self.detail {
            if self.definition != contract::HANDOFF_ARCHIVE
                && self.exception != "SkillVerificationError"
            {
                error["detail"] = json!(detail);
            }
            if !self.usage && self.exception != "SkillVerificationError" {
                error["legacy_category"] = json!("invalid-input");
            }
        }
        json!({"command": command, "data": null, "error": error, "ok": false})
    }
}

impl From<io::Error> for Error {
    fn from(error: io::Error) -> Self {
        let definition = match error.kind() {
            io::ErrorKind::NotFound => contract::MISSING,
            io::ErrorKind::PermissionDenied | io::ErrorKind::IsADirectory => contract::PERMISSION,
            _ => contract::IO,
        };
        let exception = match error.kind() {
            io::ErrorKind::NotFound => "FileNotFoundError",
            io::ErrorKind::PermissionDenied => "PermissionError",
            io::ErrorKind::IsADirectory => "IsADirectoryError",
            io::ErrorKind::AlreadyExists => "FileExistsError",
            io::ErrorKind::NotADirectory => "NotADirectoryError",
            _ => "OSError",
        };
        Self {
            exception,
            ..Self::new(definition, 1, None)
        }
    }
}
