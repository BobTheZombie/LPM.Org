use clap::ValueEnum;

/// Commands that require elevated privileges in the native implementation.
#[derive(Debug, Clone, Copy, Eq, PartialEq, ValueEnum)]
pub enum PrivilegedCommand {
    Install,
    Remove,
    Build,
}

/// Gate used by the CLI to determine whether the operation is permitted.
pub trait PrivilegeGate {
    fn is_allowed(&self, cmd: PrivilegedCommand) -> bool;
}

pub struct DefaultPrivilegeGate;

impl Default for DefaultPrivilegeGate {
    fn default() -> Self {
        Self
    }
}

impl PrivilegeGate for DefaultPrivilegeGate {
    fn is_allowed(&self, _cmd: PrivilegedCommand) -> bool {
        // For now simply require that the process is running as root.
        unsafe { libc::geteuid() == 0 }
    }
}

impl PrivilegedCommand {
    pub fn from_command(command: &crate::cli::Command) -> Option<Self> {
        match command {
            crate::cli::Command::Install(_) => Some(PrivilegedCommand::Install),
            crate::cli::Command::Remove(_) => Some(PrivilegedCommand::Remove),
            crate::cli::Command::Build(_) => Some(PrivilegedCommand::Build),
            crate::cli::Command::Gui => None,
        }
    }
}
