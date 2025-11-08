#!/usr/bin/env python3
"""CLI helper for agents to generate AI-powered responses using LLM APIs.

Usage:
    python generate_response.py --agent-name "BlackDog" \\
        --from-name "PinkPond" --subject "Strategic Planning" \\
        --body "Let's discuss Q1 strategy..." \\
        [--model "claude-3-5-sonnet-20241022"]

Returns JSON with generated response.
Reads agent configuration from agent_config.json for per-agent model settings.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mcp_agent_mail.llm import complete_system_user


# Load agent configuration
CONFIG_FILE = Path(__file__).parent / "agent_config.json"

def load_agent_config():
    """Load agent configuration from JSON file."""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"agents": {}, "defaults": {"model": "gpt-4o-mini", "temperature": 0.7, "max_tokens": 1000}}


# Agent system prompts
AGENT_PROMPTS = {
    "BlackDog": {
        "role": "CBO - Chief Brand Officer",
        "prompt": """You are BlackDog, the Chief Brand Officer (CBO) for WIN Platform.

Your responsibilities:
- Brand strategy and positioning
- Marketing campaigns and messaging
- Brand consistency across all touchpoints
- Market research and competitive analysis
- Brand voice and guidelines

Communication style:
- Strategic and visionary
- Data-driven with creative flair
- Collaborative but decisive on brand matters
- Clear, professional, and inspiring

When responding to messages:
1. Acknowledge the sender and their message
2. Provide your brand/marketing perspective
3. Offer actionable insights or recommendations
4. Keep responses concise but valuable (2-3 paragraphs)
5. Use markdown formatting for clarity
6. Sign with your role

Daily sync protocol:
- If the subject contains "Daily Sync", include sections `## Commitments` (bullet list of accepted/declined tasks with ETAs) and `## Bead Adjustments` using `- BEAD_TXN: task_id=<id> delta=<int> reason="..."` entries.
- Summarize risks/dependencies under `## Risks & Requests` if applicable.

Respond to the message below as BlackDog would."""
    },
    "GreenPresident": {
        "role": "President - Strategic Operations",
        "prompt": """You are GreenPresident, the President for WIN Platform.

Your responsibilities:
- Drive cross-functional execution of the CEO's vision
- Provide strategic alignment across departments
- Translate executive directives into actionable plans
- Coordinate stakeholder communication and priorities
- Safeguard company cadence, culture, and operational excellence

Communication style:
- Decisive, pragmatic, and outcomes-focused
- Synthesizes perspectives into clear direction
- Empowers teams while holding them accountable
- Communicates succinctly with an executive lens
- Collaborates closely with the CEO while owning day-to-day alignment

When responding to messages:
1. Acknowledge the sender and their message
2. Provide strategic direction or operational guidance
3. Clarify decisions, tradeoffs, or next steps
4. Keep responses focused on outcomes (2-3 paragraphs)
5. Use markdown formatting for clarity
6. Sign with your role

Daily sync protocol:
- If the subject contains "Daily Sync", include sections `## Commitments` (bullet list of decisions and owners with ETAs) and `## Bead Adjustments` using `- BEAD_TXN: task_id=<id> delta=<int> reason="..."` entries.
- Close with `## Risks & Requests` when executive support is required.

Respond to the message below as GreenPresident would."""
    }
}


async def generate_response(agent_name: str, from_name: str, subject: str, body: str, model: str = None, thread_depth: int = 0, max_depth: int = 3) -> dict:
    """Generate AI-powered response for an agent.

    Args:
        agent_name: Name of the agent (GreenPresident, BlackDog, etc.)
        from_name: Name of the message sender
        subject: Message subject
        body: Message body
        model: Optional model override. If not provided, uses agent config or defaults to gpt-4o-mini
        thread_depth: Current depth of the conversation thread (for loop awareness)
        max_depth: Maximum allowed depth before conversation stops

    Returns:
        Dict with success status and response details
    """

    # Load configuration
    config = load_agent_config()

    # Get agent prompt
    agent_info = AGENT_PROMPTS.get(agent_name)
    if not agent_info:
        return {
            "error": f"Unknown agent: {agent_name}",
            "agent_name": agent_name
        }

    # Get model settings from config
    agent_config = config.get("agents", {}).get(agent_name, {})
    defaults = config.get("defaults", {})

    # Determine which model to use (priority: CLI arg > agent config > defaults)
    use_model = model or agent_config.get("model") or defaults.get("model", "gpt-4o-mini")
    use_temperature = agent_config.get("temperature", defaults.get("temperature", 0.7))
    use_max_tokens = agent_config.get("max_tokens", defaults.get("max_tokens", 1000))

    system_prompt = agent_info["prompt"]

    # Add loop awareness context
    loop_context = ""
    if thread_depth > 0:
        loop_context = f"""

**Conversation Context:** This is message {thread_depth + 1} in an ongoing thread (limit: {max_depth} messages). """
        if thread_depth >= max_depth - 1:
            loop_context += "This is the final response in this thread - be concise and conclusive."
        elif thread_depth >= (max_depth * 0.6):
            loop_context += "Approaching thread limit - focus on key points and actionable items."

    # Construct user message
    user_message = f"""From: {from_name}
Subject: {subject}

{body}{loop_context}

---

Generate a response as {agent_name} ({agent_info['role']})."""

    try:
        # Call LLM API (via LiteLLM - supports OpenAI, Anthropic, etc.)
        result = await complete_system_user(
            system=system_prompt,
            user=user_message,
            model=use_model,
            temperature=use_temperature,
            max_tokens=use_max_tokens
        )

        return {
            "success": True,
            "agent_name": agent_name,
            "role": agent_info["role"],
            "response_body": result.content,
            "model": result.model,
            "provider": result.provider
        }
    except Exception as e:
        return {
            "error": str(e),
            "agent_name": agent_name,
            "details": "Check ANTHROPIC_API_KEY in .env file"
        }


def main():
    parser = argparse.ArgumentParser(
        description="Generate AI-powered agent responses",
        epilog="Model is read from agent_config.json unless overridden with --model"
    )
    parser.add_argument("--agent-name", required=True, help="Agent name (GreenPresident, BlackDog)")
    parser.add_argument("--from-name", required=True, help="Sender name")
    parser.add_argument("--subject", required=True, help="Message subject")
    parser.add_argument("--body", required=True, help="Message body")
    parser.add_argument("--model", help="LLM model to use (overrides agent_config.json)")
    parser.add_argument("--thread-depth", type=int, default=0, help="Current thread depth (for loop awareness)")
    parser.add_argument("--max-depth", type=int, default=3, help="Maximum thread depth before stopping")

    args = parser.parse_args()

    # Run async function
    result = asyncio.run(generate_response(
        agent_name=args.agent_name,
        from_name=args.from_name,
        subject=args.subject,
        body=args.body,
        model=args.model,
        thread_depth=args.thread_depth,
        max_depth=args.max_depth
    ))

    # Output JSON
    print(json.dumps(result, indent=2))

    # Exit with error code if failed
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
