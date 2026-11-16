fn main() -> std::process::ExitCode {
    mdm_cli::main(std::env::args_os().skip(1).collect())
}
