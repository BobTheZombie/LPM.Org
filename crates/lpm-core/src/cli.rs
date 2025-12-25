use clap::{Args, Parser, Subcommand};
use log::warn;

use crate::privileges::{PrivilegeGate, PrivilegedCommand};

/// Representation of the lpm command line options.
#[derive(Debug, Parser, Clone)]
#[command(author, version, about = "Rust port of the LPM command interface.")]
pub struct LpmCli {
    /// Execute in sysconfig mode. Mutually exclusive with --root.
    #[arg(long, conflicts_with = "root")]
    pub sysconfig: bool,

    /// Alternate installation root.
    #[arg(long)]
    pub root: Option<String>,

    /// Run the first run wizard.
    #[arg(long)]
    pub first_run: bool,

    #[command(subcommand)]
    pub command: Option<Command>,
}

#[derive(Debug, Subcommand, Clone)]
pub enum Command {
    /// Install one or more packages.
    Install(PackageArgs),
    /// Remove one or more packages.
    Remove(PackageArgs),
    /// Build a package from the current directory.
    Build(BuildArgs),
    /// Invoke the GUI frontend if available.
    Gui,
}

#[derive(Debug, Args, Clone)]
pub struct PackageArgs {
    #[arg(required = true)]
    pub packages: Vec<String>,
}

#[derive(Debug, Args, Clone)]
pub struct BuildArgs {
    #[arg(long, default_value = "release")]
    pub profile: String,
}

/// Result of dispatching a CLI invocation.
pub enum DispatchResult {
    /// Command handled successfully.
    Success,
    /// A preflight check failed.
    Blocked(String),
    /// The GUI should be launched instead of CLI handling.
    GuiRequested,
}

impl LpmCli {
    /// Handle privilege and first run logic used by wrapper scripts.
    pub fn dispatch(self, gate: &impl PrivilegeGate, first_run_hook: impl Fn() -> bool) -> DispatchResult {
        if self.first_run {
            if first_run_hook() {
                return DispatchResult::Success;
            }
        }

        let command = match self.command {
            Some(cmd) => cmd,
            None => return DispatchResult::GuiRequested,
        };

        if let Some(kind) = PrivilegedCommand::from_command(&command) {
            if !gate.is_allowed(kind) {
                return DispatchResult::Blocked("command requires elevated privileges".into());
            }
        }

        match command {
            Command::Install(_) | Command::Remove(_) | Command::Build(_) | Command::Gui => {}
        }

        if self.sysconfig && self.root.is_some() {
            warn!("--sysconfig is exclusive with --root; using sysconfig");
        }

        DispatchResult::Success
    }
}
