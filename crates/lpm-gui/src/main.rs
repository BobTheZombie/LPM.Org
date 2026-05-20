use clap::Parser;
use lpm_core::cli::LpmCli;

/// Minimal GUI entry point placeholder.
#[derive(Debug, Parser)]
struct GuiArgs {}

fn main() {
    let _ = GuiArgs::parse();
    println!("Launching placeholder GUI. Integration with lpm-core to follow.");
    // In a real implementation, this would spin up a native windowing
    // toolkit (e.g. Tauri or Qt bindings) and delegate to the CLI facade.
}
