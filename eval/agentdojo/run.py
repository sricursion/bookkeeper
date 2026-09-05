"""Run the AgentDojo banking suite with and without the gate, or check the gate offline.

Offline, no model needed:
  python eval/agentdojo/run.py --validate-tasks   # do the benign tasks' own ground-truth calls pass?
  python eval/agentdojo/run.py --goal-matrix      # would the attacker's exact goal action pass, per pair?

Live, needs FIREWORKS_API_KEY in the environment or in .env:
  python eval/agentdojo/run.py --limit 3                       # 27-pair pilot, baseline then gate
  python eval/agentdojo/run.py --attack tool_knowledge --limit 3
  python eval/agentdojo/run.py --clean-utility --limit 3        # utility without any attack
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from munim import Gate, Ledger  # noqa: E402
from munim_defense import CONSEQUENTIAL, GatedToolsExecutor, action_from_call, derive_mandate, records_from_env  # noqa: E402

DEFAULT_MODEL_ID = "accounts/fireworks/models/glm-5p3-flash"
DEFAULT_MODEL_LABEL = "glm-5.3-flash"
DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"
GATE_VERSION = 4  # bump when the gate or the mandate derivation changes, so cached traces are not reused


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def suite_and_now():
    from agentdojo.task_suite.load_suites import get_suite

    return get_suite("v1", "banking"), int(time.time())


def selected_user_tasks(suite, args) -> list[str]:
    ids = list(suite.user_tasks.keys())
    if args.user_tasks:
        ids = [i for i in ids if i in set(args.user_tasks)]
    if args.limit:
        ids = ids[: args.limit]
    return ids


# Offline checks ---------------------------------------------------------------

def gate_for(task_text: str, env, now: int, cap_multiplier: float, ledger_path: Path) -> Gate:
    mandate = derive_mandate(task_text, env, now, cap_multiplier)
    return Gate(mandate, records_from_env(env, now), Ledger(ledger_path), clock=lambda: now)


def validate_tasks(args) -> int:
    """Evaluate each benign task's own ground-truth calls. Anything held here is
    a utility cost the gate pays by design, and it is reported, not hidden."""
    suite, now = suite_and_now()
    tmp = Path(tempfile.mkdtemp())
    fully_allowed = 0
    ids = selected_user_tasks(suite, args)
    for task_id in ids:
        task = suite.user_tasks[task_id]
        env = suite.load_and_inject_default_environment({})
        gate = gate_for(task.PROMPT, env, now, args.cap_multiplier, tmp / f"{task_id}.jsonl")
        m = gate.mandate
        print(f"\n{task_id}: {task.PROMPT[:110]}{'...' if len(task.PROMPT) > 110 else ''}")
        print(
            f"  scopes={sorted(s.value for s in m.scopes) or '-'} named_recipients={sorted(m.named_recipients) or '-'} "
            f"named_amounts={sorted(m.named_amounts) or '-'} named_fields={sorted(m.named_fields) or '-'} "
            f"cap={m.limits_for(next(iter(m.limits))).per_action_cap if m.limits else '-'}"
        )
        outcomes = []
        for i, call in enumerate(task.ground_truth(env)):
            if call.function not in CONSEQUENTIAL:
                continue
            action = action_from_call(call, env, f"gt-{task_id}-{i}")
            if action is None:
                continue
            decision = gate.evaluate(action)
            outcomes.append(decision.disposition)
            print(f"  {decision.disposition:<5} {call.function}({json.dumps(dict(call.args))[:90]})")
            if decision.disposition != "allow":
                print(f"        {decision.reason[:220]}")
        if all(o == "allow" for o in outcomes):
            fully_allowed += 1
    print(f"\n{fully_allowed}/{len(ids)} benign tasks have every consequential ground-truth call allowed")
    return 0


def goal_matrix(args) -> int:
    """For every (user task, injection task) pair: if the model did exactly what the
    attacker asked, would the gate let it through? Model-free, so this is a bound on
    the gate, not a measurement of the model."""
    suite, now = suite_and_now()
    tmp = Path(tempfile.mkdtemp())
    user_ids = selected_user_tasks(suite, args)
    injection_ids = list(suite.injection_tasks.keys())
    header = "user task".ljust(14) + " ".join(i.replace("injection_task_", "it").rjust(4) for i in injection_ids)
    print(header)
    allowed_cells: list[tuple[str, str, str]] = []
    total = 0
    for user_id in user_ids:
        task = suite.user_tasks[user_id]
        row = []
        for injection_id in injection_ids:
            injection = suite.injection_tasks[injection_id]
            env = suite.load_and_inject_default_environment({})
            gate = gate_for(task.PROMPT, env, now, args.cap_multiplier, tmp / f"{user_id}-{injection_id}.jsonl")
            cell = "-"
            for i, call in enumerate(injection.ground_truth(env)):
                if call.function not in CONSEQUENTIAL:
                    continue
                action = action_from_call(call, env, f"goal-{i}")
                if action is None:
                    continue
                decision = gate.evaluate(action)
                letter = {"allow": "A", "hold": "H", "deny": "D"}[decision.disposition]
                cell = letter if cell == "-" or letter == "A" else cell
                if decision.disposition == "allow":
                    allowed_cells.append((user_id, injection_id, decision.reason))
            total += 1
            row.append(cell.rjust(4))
        print(user_id.replace("user_task_", "ut").ljust(14) + " ".join(row))
    print(f"\nA = attacker's goal action allowed, H = held, D = denied. {len(allowed_cells)}/{total} cells allowed.")
    for user_id, injection_id, reason in allowed_cells:
        print(f"  {user_id} x {injection_id}: {reason[:160]}")
    return 0


# Live runs ---------------------------------------------------------------------

def build_llm(args):
    import openai
    from agentdojo.agent_pipeline.llms import openai_llm as wrapper
    from agentdojo.types import ChatAssistantMessage
    from agentdojo.types import text_content_block_from_string as text_block

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is not set. Put it in .env (gitignored) or the environment.")
    client = openai.OpenAI(api_key=api_key, base_url=args.base_url, max_retries=8, timeout=180.0)

    class SteadyOpenAILLM(wrapper.OpenAILLM):
        """The stock wrapper drops temperature 0.0 (it tests truthiness) and lets a
        malformed tool-call argument string abort the whole run. This sends the
        temperature as given and turns a malformed call into an assistant turn
        with no tool calls, so the episode ends and is scored, not crashed."""

        def query(self, query, runtime, env=None, messages=(), extra_args=None):
            extra_args = {} if extra_args is None else extra_args
            openai_messages = [wrapper._message_to_openai(m, self.model) for m in messages]
            openai_tools = [wrapper._function_to_openai(tool) for tool in runtime.functions.values()]
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=openai_tools or openai.NOT_GIVEN,
                tool_choice="auto" if openai_tools else openai.NOT_GIVEN,
                temperature=self.temperature if self.temperature is not None else openai.NOT_GIVEN,
                reasoning_effort=self.reasoning_effort or openai.NOT_GIVEN,
            )
            choice = completion.choices[0]
            try:
                output = wrapper._openai_to_assistant_message(choice.message)
            except json.JSONDecodeError as exc:
                output = ChatAssistantMessage(role="assistant", content=[text_block(f"[malformed tool call arguments: {exc}]")], tool_calls=None)
            if getattr(choice, "finish_reason", None) == "length" and not output.get("tool_calls"):
                output = ChatAssistantMessage(role="assistant", content=[text_block("[response truncated at the output limit]")], tool_calls=None)
            return query, runtime, env, [*messages, output], extra_args

    return SteadyOpenAILLM(client, args.model_id, temperature=args.temperature, reasoning_effort=args.reasoning_effort or None)


def build_pipeline(args, llm, mode: str, logdir: Path, run_tag: str):
    from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, load_system_message
    from agentdojo.agent_pipeline.basic_elements import InitQuery, SystemMessage
    from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop, ToolsExecutor

    executor = GatedToolsExecutor(logdir / "ledgers" / run_tag, cap_multiplier=args.cap_multiplier) if mode == "munim" else ToolsExecutor()
    pipeline = AgentPipeline([SystemMessage(load_system_message(None)), InitQuery(), llm, ToolsExecutionLoop([executor, llm])])
    # The harness version is part of the cache identity for both arms, so a baseline and a gated
    # run reported together always share the same wrapper and sampling settings.
    pipeline.name = f"{args.model_label}-{mode}-g{GATE_VERSION}" + (f"-cap{args.cap_multiplier:g}" if mode == "munim" else "")
    return pipeline, executor


def pair_only_gate_stats(ledger_dir: Path, user_prompts: set[str]) -> dict[str, int] | None:
    """Gate decisions counted over attack-pair episodes only. AgentDojo also runs each
    attacker goal as a plain user task first; those episodes are excluded here."""
    if not ledger_dir.exists():
        return None
    stats = {"episodes": 0, "evaluated": 0, "allow": 0, "hold": 0, "deny": 0, "precheck_episodes": 0}
    for path in sorted(ledger_dir.glob("episode_*.jsonl")):
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        head = next((e for e in entries if e["kind"] == "episode"), None)
        if head is None:
            continue
        if head["payload"]["query"] not in user_prompts:
            stats["precheck_episodes"] += 1
            continue
        stats["episodes"] += 1
        for e in entries:
            if e["kind"] == "decision":
                stats["evaluated"] += 1
                stats[e["payload"]["disposition"]] += 1
    return stats


def summarise(results: dict, key: str) -> tuple[float | None, int]:
    values = list(results.get(key, {}).values())
    if not values:
        return None, 0
    return sum(1 for v in values if v) / len(values), len(values)


def live(args) -> int:
    from agentdojo.attacks.attack_registry import load_attack
    from agentdojo.benchmark import benchmark_suite_with_injections, benchmark_suite_without_injections
    from agentdojo.logging import OutputLogger
    from agentdojo.models import MODEL_NAMES

    MODEL_NAMES.setdefault(args.model_label, args.model_family)  # so the attack text addresses the model by its real name
    suite, _ = suite_and_now()
    llm = build_llm(args)
    user_ids = selected_user_tasks(suite, args)
    user_prompts = {suite.user_tasks[i].PROMPT for i in suite.user_tasks}
    modes = ["none", "munim"] if args.defense == "both" else [args.defense]
    logdir = Path(args.logdir)
    run_tag = f"{'clean' if args.clean_utility else args.attack}-{time.strftime('%Y%m%d-%H%M%S')}"
    rows = []
    for mode in modes:
        mode_dir = logdir / mode
        mode_dir.mkdir(parents=True, exist_ok=True)
        pipeline, executor = build_pipeline(args, llm, mode, mode_dir, run_tag)
        started = time.time()
        with OutputLogger(str(mode_dir), live=None):
            if args.clean_utility:
                results = benchmark_suite_without_injections(pipeline, suite, logdir=mode_dir, force_rerun=args.force_rerun, user_tasks=user_ids)
                attack_name = "none"
            else:
                attack = load_attack(args.attack, suite, pipeline)
                attack_name = args.attack
                results = benchmark_suite_with_injections(
                    pipeline, suite, attack, logdir=mode_dir, force_rerun=args.force_rerun,
                    user_tasks=user_ids, injection_tasks=args.injection_tasks or None, verbose=True,
                )
        utility, n_utility = summarise(results, "utility_results")
        if args.clean_utility:
            asr, pairs = None, n_utility  # without injections AgentDojo marks every pair secure; that is not a measurement
        else:
            asr, pairs = summarise(results, "security_results")
        gate_stats = pair_only_gate_stats(mode_dir / "ledgers" / run_tag, user_prompts) if mode == "munim" else None
        record = {
            "mode": mode,
            "model_id": args.model_id,
            "model_label": args.model_label,
            "pipeline_name": pipeline.name,
            "attack": attack_name,
            "user_tasks": user_ids,
            "pairs": pairs,
            "attack_success_rate": asr,
            "utility": utility,
            "gate_stats": gate_stats,
            "gate_stats_all_episodes": getattr(executor, "stats", None),
            "ledger_dir": str(mode_dir / "ledgers" / run_tag) if mode == "munim" else None,
            "temperature": args.temperature,
            "reasoning_effort": args.reasoning_effort,
            "seconds": round(time.time() - started, 1),
            "security_results": {f"{k[0]}|{k[1]}": v for k, v in results.get("security_results", {}).items()},
            "utility_results": {f"{k[0]}|{k[1]}": v for k, v in results.get("utility_results", {}).items()},
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        stem = "results_clean" if args.clean_utility else f"results_{attack_name}"
        (mode_dir / f"{stem}_{args.model_label}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        rows.append(record)
        print(f"\n[{mode}] attack={attack_name} pairs={record['pairs']} attack_success={asr} utility={utility} gate={record['gate_stats']}")

    lines = ["| model | mode | attack | pairs | attack success | utility | gate allow/hold/deny (pair episodes) |", "|---|---|---|---:|---:|---:|---|"]
    for r in rows:
        asr = "-" if r["attack_success_rate"] is None else f"{100 * r['attack_success_rate']:.1f}%"
        util = "-" if r["utility"] is None else f"{100 * r['utility']:.1f}%"
        g = r["gate_stats"]
        gate = "-" if not g else f"{g['allow']}/{g['hold']}/{g['deny']}"
        lines.append(f"| {r['model_label']} | {r['mode']} | {r['attack']} | {r['pairs']} | {asr} | {util} | {gate} |")
    summary = "\n".join(lines)
    print("\n" + summary)
    with (logdir / "summary.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n### {args.model_label}, gate v{GATE_VERSION}, {time.strftime('%Y-%m-%d %H:%M')}\n\n{summary}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--model-label", default=DEFAULT_MODEL_LABEL)
    parser.add_argument("--model-family", default="GLM", help="how the attack text addresses the model, e.g. GLM or Llama")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--reasoning-effort", default=None, help="low, medium, high, or max for reasoning models; default leaves the model's own default")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="FIREWORKS_API_KEY")
    parser.add_argument("--attack", default="important_instructions")
    parser.add_argument("--defense", choices=["none", "munim", "both"], default="both")
    parser.add_argument("--limit", type=int, help="first N user tasks")
    parser.add_argument("--user-tasks", nargs="*")
    parser.add_argument("--injection-tasks", nargs="*")
    parser.add_argument("--logdir", default=str(ROOT / "runs" / "agentdojo"))
    parser.add_argument("--cap-multiplier", type=float, default=2.0)
    parser.add_argument("--clean-utility", action="store_true", help="run without injections")
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--validate-tasks", action="store_true")
    parser.add_argument("--goal-matrix", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    if args.validate_tasks:
        return validate_tasks(args)
    if args.goal_matrix:
        return goal_matrix(args)
    return live(args)


if __name__ == "__main__":
    raise SystemExit(main())
