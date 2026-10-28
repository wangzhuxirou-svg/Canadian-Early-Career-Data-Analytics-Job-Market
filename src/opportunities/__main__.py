import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Instagram opportunity tools.")
    commands = (
        "collect", "ocr", "extract", "review", "run", "schedule", "report",
        "recover-lock", "triage",
    )
    parser.add_argument("command", choices=commands)
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        parser.parse_args()
    arguments = parser.parse_args([sys.argv[1]])

    if arguments.command == "triage":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from src.opportunities.triage import main as command
    elif arguments.command == "collect":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from src.opportunities.collect import main as command
    elif arguments.command == "ocr":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from src.opportunities.ocr import main as command
    elif arguments.command == "extract":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from src.opportunities.extract import main as command
    elif arguments.command == "review":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from src.opportunities.review import main as command
    elif arguments.command in {"run", "schedule", "report", "recover-lock"}:
        from src.opportunities.cli import main as command
    else:
        parser.error(f"Unsupported command: {arguments.command}")
    raise SystemExit(command())


if __name__ == "__main__":
    main()
