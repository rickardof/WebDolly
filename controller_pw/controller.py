#!/usr/bin/env python3
"""CLI wrapper around agent_core.run_agent.

Usage:
  ./controller.py "Click the BLUE button." --url file:///data/test.html
"""

import argparse

from agent_core import run_agent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--url", default="")
    parser.add_argument("--max-rounds", type=int, default=10)
    cli_args = parser.parse_args()

    gen = run_agent(cli_args.task, cli_args.url, cli_args.max_rounds)
    try:
        event = next(gen)
    except StopIteration:
        return

    while True:
        if event["type"] == "step":
            step = event["step"]
            print(f"[step {step}] thoughts: {event['thoughts'][:200]!r}")
            print(f"[step {step}] action: {event['action_type']} {event['args']}")
            print(f"[step {step}] executed: {event['result_desc']}")
        elif event["type"] == "question":
            # No CLI input channel for this -- matches the old behavior of
            # stopping without printing a "DONE:" line (survey_loop.py's
            # parser treats that as an error outcome and retries later).
            print(f"AGENT ASKS: {event['question']}")
            break
        elif event["type"] == "done":
            print(f"DONE: {event['answer']}")
            break
        elif event["type"] == "error":
            print(f"ERROR: {event['message']}")
            break

        try:
            event = next(gen)
        except StopIteration:
            break


if __name__ == "__main__":
    main()
