use clap::Parser;
use lpm_core::cli::{DispatchResult, LpmCli};
use lpm_core::privileges::DefaultPrivilegeGate;

fn main() {
    env_logger::init();
    let cli = LpmCli::parse();
    let gate = DefaultPrivilegeGate::default();

    let status = match cli.dispatch(&gate, run_first_run_wizard) {
        DispatchResult::Success => 0,
        DispatchResult::Blocked(reason) => {
            eprintln!("lpm: {reason}");
            2
        }
        DispatchResult::GuiRequested => {
            println!("launching GUI frontend...");
            0
        }
    };

    std::process::exit(status);
}

fn run_first_run_wizard() -> bool {
    println!("Running first run wizard (stub)");
    true
}
