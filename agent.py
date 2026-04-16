import json
import os
import re
from argparse import ArgumentParser
from urllib.parse import quote
from typing import Any, Dict, List

from dotenv import load_dotenv
from groq import Groq

from playwright.sync_api import Locator, Page, sync_playwright


TARGET_URL = "file:///C:/Users/Lenovo/Documents/GitHub/AiAdmin/frontend/index.html"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"

ALLOWED_ACTION_SCHEMA = """
Allowed actions (exactly one object):
1) {"action": "click", "label": "..."}
2) {"action": "click", "label": "...", "row_contains": "..."}
3) {"action": "type", "label": "...", "text": "...", "press_enter": true}
4) {"action": "select", "label": "...", "value": "..."}
5) {"action": "wait", "seconds": 1}
6) {"action": "finish", "reason": "..."}
7) {"action": "fail", "reason": "..."}
"""


def observe_page(page: Page) -> Dict[str, str]:
    """Return current page state for planning and lightweight validation."""
    observation: Dict[str, str] = {
        "title": "",
        "url": "",
        "visible_text": "",
        "search_input_value": "",
        "new_password_value": "",
        "license_value": "",
    }

    if page.is_closed():
        observation["visible_text"] = "page closed"
        return observation

    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        # Navigation may still be settling after actions like delete.
        pass

    try:
        observation["title"] = page.title()
    except Exception:
        observation["title"] = ""

    try:
        observation["url"] = page.url
    except Exception:
        observation["url"] = ""

    try:
        observation["visible_text"] = page.locator("body").inner_text()
    except Exception:
        observation["visible_text"] = ""

    # Keep observation simple, but include key field values used by validator.
    search_candidates = [
        page.get_by_label("Search users", exact=False),
        page.get_by_placeholder("Search users", exact=False),
        page.get_by_placeholder("Search by email", exact=False),
        page.locator("#searchEmail"),
    ]
    for locator in search_candidates:
        if locator.count() > 0:
            try:
                observation["search_input_value"] = locator.first.input_value()
            except Exception:
                pass
            break

    password_candidates = [
        page.get_by_placeholder("New password", exact=False),
        page.locator("#newPassword"),
    ]
    for locator in password_candidates:
        if locator.count() > 0:
            try:
                observation["new_password_value"] = locator.first.input_value()
            except Exception:
                pass
            break

    license_candidates = [
        page.get_by_label("License", exact=False),
        page.get_by_label("Assign / Change License", exact=False),
        page.locator("#license"),
        page.locator("#licenseType"),
    ]
    for locator in license_candidates:
        if locator.count() > 0:
            try:
                observation["license_value"] = locator.first.input_value()
            except Exception:
                pass
            break

    return observation


def validate_action_result(
    action: Dict[str, Any],
    previous_observation: Dict[str, str],
    new_observation: Dict[str, str],
) -> Dict[str, Any]:
    """
    Lightweight rule-based post-action validator.

    Validation is intentionally simple: it checks visible UI effects to reduce
    silent failures without adding heavy frameworks or extra LLM calls.
    """
    action_name = action.get("action")
    label = str(action.get("label", "")).strip().lower()

    if action_name in {"wait", "finish", "fail"}:
        return {"ok": True, "reason": "No validation needed for terminal/wait action"}

    if action_name == "select":
        expected_value = str(action.get("value", "")).strip()
        current_value = new_observation.get("license_value", "").strip()
        if expected_value and current_value == expected_value:
            return {"ok": True, "reason": "Dropdown selection updated"}
        return {"ok": False, "reason": "Dropdown selection did not update"}

    if action_name == "click" and label in {"users", "search users"}:
        is_users_page = (
            "search-users.html" in new_observation["url"].lower()
            or "search users" in new_observation["title"].lower()
            or "search users" in new_observation["visible_text"].lower()
        )
        if is_users_page:
            return {"ok": True, "reason": "Users page detected after click"}
        return {"ok": False, "reason": "Users page not detected after clicking Users"}

    if action_name == "type" and label == "search users":
        typed_text = str(action.get("text", "")).strip()
        search_value = new_observation.get("search_input_value", "").strip()
        if typed_text and (typed_text in new_observation["visible_text"] or search_value == typed_text):
            return {"ok": True, "reason": "Search users input updated"}
        return {"ok": False, "reason": "Search users input did not update as expected"}

    if action_name == "click" and label == "search":
        target_email = previous_observation.get("search_input_value", "").strip()
        if target_email and target_email in new_observation["visible_text"]:
            return {"ok": True, "reason": "Search results include target email"}
        return {"ok": False, "reason": "Search results do not show target email"}

    if action_name == "click" and label == "view" and action.get("row_contains"):
        target_email = str(action.get("row_contains", "")).strip()
        is_profile = (
            "user-profile.html" in new_observation["url"].lower()
            or "user profile" in new_observation["visible_text"].lower()
        )
        if is_profile and target_email and target_email in new_observation["visible_text"]:
            return {"ok": True, "reason": "Opened matching user profile"}
        return {"ok": False, "reason": "User profile not opened for requested row"}

    if action_name == "type" and label == "new password":
        typed_text = str(action.get("text", ""))
        value = new_observation.get("new_password_value", "")
        if typed_text and value == typed_text:
            return {"ok": True, "reason": "New password field updated"}
        return {"ok": False, "reason": "New password field did not update"}

    if action_name == "click" and label == "reset password":
        if "password reset successful" in new_observation["visible_text"].lower():
            return {"ok": True, "reason": "Password reset success message detected"}
        return {"ok": False, "reason": "Password reset success message not found"}

    if action_name == "click" and label == "save license":
        selected_license = previous_observation.get("license_value", "").strip() or new_observation.get("license_value", "").strip()
        visible_text = new_observation["visible_text"].lower()
        if (
            "license assigned successfully" in visible_text
            or (selected_license and f"license: {selected_license}" in visible_text)
        ):
            return {"ok": True, "reason": "License change saved"}
        return {"ok": False, "reason": "License save confirmation not found"}

    if action_name == "click" and label == "create user":
        previous_url = previous_observation.get("url", "").lower()
        new_url = new_observation.get("url", "").lower()
        if "index.html" in previous_url and "create-user.html" in new_url:
            return {"ok": True, "reason": "Opened create user page"}
        visible_text = new_observation["visible_text"].lower()
        if "user created successfully" in visible_text or "user-profile.html" in new_observation["url"].lower():
            return {"ok": True, "reason": "Create user completed"}
        return {"ok": False, "reason": "Create user confirmation not found"}

    if action_name == "click" and label == "delete user":
        visible_text = new_observation["visible_text"].lower()
        url = new_observation["url"].lower()
        # Wait for actual redirect to search-users page, not just success message visibility
        if "search-users.html" in url or ("user deleted successfully" in visible_text and "search users" in visible_text):
            return {"ok": True, "reason": "User deletion confirmed and redirected"}
        if "user deleted successfully" in visible_text:
            # Success message visible but not yet redirected
            return {"ok": False, "reason": "Deletion successful but page redirecting"}

    return {"ok": True, "reason": "No specific validation rule for action"}


def _is_retryable_action(action: Dict[str, Any]) -> bool:
    """Allow one retry for non-terminal UI interactions."""
    return action.get("action") in {"click", "type"}


def _extract_requested_license(user_request: str) -> str:
    """Pull a license choice from the user's request when one is mentioned."""
    lowered = user_request.lower()
    for license_name in ("plus", "premium", "basic"):
        if re.search(rf"\b{license_name}\b", lowered):
            return license_name
    return "basic"


def _extract_requested_email(user_request: str) -> str:
    """Pull the first email address from the user's request."""
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", user_request)
    return match.group(0) if match else "newworker@example.com"


def _fallback_plan_next_action(
    user_request: str,
    observation: Dict[str, str],
    history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Deterministic backup planner for simple create/profile license flows."""
    request_lower = user_request.lower()
    visible_text = observation.get("visible_text", "").lower()
    url = observation.get("url", "").lower()

    if "license" not in request_lower:
        return {"action": "fail", "reason": "Unable to plan next action"}

    requested_license = _extract_requested_license(user_request)

    if "create user" in visible_text or "create-user.html" in url:
        typed_email = any(action.get("action") == "type" and action.get("label", "").lower() == "email" for action in history)
        typed_password = any(action.get("action") == "type" and action.get("label", "").lower() == "password" for action in history)
        selected_license = any(action.get("action") == "select" for action in history)

        if not typed_email:
            return {"action": "type", "label": "Email", "text": _extract_requested_email(user_request), "press_enter": False}
        if not typed_password:
            return {"action": "type", "label": "Password", "text": "pass123", "press_enter": False}
        if not selected_license:
            return {"action": "select", "label": "License", "value": requested_license}
        return {"action": "click", "label": "Create User"}

    if "user profile" in visible_text or "user-profile.html" in url:
        selected_license = any(action.get("action") == "select" for action in history)
        clicked_save = any(
            action.get("action") == "click" and str(action.get("label", "")).strip().lower() == "save license"
            for action in history
        )

        if not selected_license:
            return {"action": "select", "label": "Assign / Change License", "value": requested_license}
        if not clicked_save:
            return {"action": "click", "label": "Save License"}
        return {"action": "finish", "reason": f"License set to {requested_license}"}

    if "search-users.html" in url or "search users" in visible_text:
        return {"action": "click", "label": "Users"}

    if "index.html" in url or "dashboard" in visible_text:
        return {"action": "click", "label": "Create User"}

    return {"action": "fail", "reason": "Unable to infer a safe fallback action"}


def get_groq_client() -> Groq:
    """
    Create a Groq client using env vars loaded from .env.

    Groq is used here for fast planner inference while Playwright execution
    remains deterministic and controllable.
    """
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to your environment or .env file.")
    return Groq(api_key=api_key)


def _extract_json_object(text: str) -> str:
    """Extract the first JSON object block from model output."""
    stripped = text.strip()

    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in LLM output")
    return stripped[start : end + 1]


def _validate_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize one planner action object."""
    action_name = action.get("action")
    if action_name not in {"click", "type", "wait", "finish", "fail"}:
        raise ValueError("Invalid action type")

    if action_name == "click":
        label = action.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Click action requires non-empty label")
        normalized: Dict[str, Any] = {"action": "click", "label": label.strip()}
        row_contains = action.get("row_contains")
        if row_contains is not None:
            if not isinstance(row_contains, str) or not row_contains.strip():
                raise ValueError("row_contains must be a non-empty string")
            normalized["row_contains"] = row_contains.strip()
        return normalized

    if action_name == "type":
        label = action.get("label")
        text = action.get("text")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Type action requires non-empty label")
        if not isinstance(text, str):
            raise ValueError("Type action requires text string")
        press_enter = bool(action.get("press_enter", False))
        return {
            "action": "type",
            "label": label.strip(),
            "text": text,
            "press_enter": press_enter,
        }

    if action_name == "select":
        label = action.get("label")
        value = action.get("value")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Select action requires non-empty label")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Select action requires non-empty value")
        return {
            "action": "select",
            "label": label.strip(),
            "value": value.strip(),
        }

    if action_name == "wait":
        seconds = action.get("seconds", 1)
        if not isinstance(seconds, (int, float)):
            raise ValueError("Wait action seconds must be numeric")
        return {"action": "wait", "seconds": float(seconds)}

    reason = action.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        reason = "No reason provided"
    return {"action": action_name, "reason": reason.strip()}


def _parse_and_validate_action(raw_text: str) -> Dict[str, Any]:
    """Parse raw model output and validate action schema."""
    json_text = _extract_json_object(raw_text)
    parsed = json.loads(json_text)
    if not isinstance(parsed, dict):
        raise ValueError("Action payload must be a JSON object")
    return _validate_action(parsed)


def plan_next_action(user_request: str, observation: Dict[str, str], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    LLM-based planner using Groq.

    The planner decides one semantic UI action from current observation.
    The executor stays deterministic so browser interactions remain reliable.
    This separation matches browser-use style execution: model plans, code acts.
    """
    recent_history = history[-8:]
    model_name = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)

    system_prompt = (
       """You are a browser UI planning engine.

Return exactly one JSON object and nothing else.

You may only return one of these actions:
- {"action":"click","label":"..."}
- {"action":"type","label":"...","text":"..."}
- {"action":"select","label":"...","option":"..."}
- {"action":"finish"}
- {"action":"fail","reason":"..."}

Page model:

Dashboard:
- buttons: "Search users", "Create user"

Search Users page:
- navigation: "Dashboard"
- input: "Search users"
- button: "Search"
- dynamic results:
  - user row containing email
  - row button: "View"

User Profile page:
- fields shown: email, license details
- button: "Reset password"
- input: "New password"
- dropdown: "Assign/change license" with options ["basic", "premium", "plus"]
- button: "Delete user"
- navigation: "Dashboard"

Create User page:
- navigation: "Dashboard"
- input: "Email"
- input: "Password"
- dropdown: "License"
- button: "Create user"

Planning policy:
- For "delete user <email>":
  1. go to Search Users if not already there
  2. type the email in "Search users"
  3. click "Search"
  4. when a matching row is visible, click "View"
  5. click "Delete user"
  
- For "reset password for <email>":
  1. search for the user
  2. open profile
  3. type the new password
  4. click "Reset password"

- For "change license for <email> to <plan>":
  1. search for the user
  2. open profile
  3. select the plan from the dropdown

Rules:
- If a field labeled "Search users" is available and the user needs to find a user, use a type action for that field.
- If the target user appears in results, prefer clicking "View" in the matching row.
- If the task mentions a license, use a select action to choose the dropdown value before clicking the save/create button.
- The license dropdown values are "basic", "premium", and "plus".
- If "No users found" is visible, treat that as a meaningful state and branch appropriately.
- Do not keep repeating wait actions if the page state is unchanged.
- When creating a new user default password is "pass123" when no password is provided.
- When resetting password for a user the default password is "newpass123" when no password is provided.
- Finish only when the request is clearly completed.
- Return fail if the task cannot continue safely or meaningfully."""
    )

    user_prompt = (
        "User request:\n"
        f"{user_request}\n\n"
        "Current observation:\n"
        f"Title: {observation['title']}\n"
        f"URL: {observation['url']}\n"
        f"Visible text:\n{observation['visible_text']}\n\n"
        "Recent action history (most recent last):\n"
        f"{json.dumps(recent_history, ensure_ascii=True)}\n\n"
        f"{ALLOWED_ACTION_SCHEMA}\n"
        "Return only JSON."
    )

    try:
        client = get_groq_client()
    except Exception as exc:
        return {"action": "fail", "reason": f"Groq client error: {exc}"}

    for _attempt in range(2):
        try:
            completion = client.chat.completions.create(
                model=model_name,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw_output = completion.choices[0].message.content or ""
            return _parse_and_validate_action(raw_output)
        except Exception:
            continue

    return _fallback_plan_next_action(user_request, observation, history)


def _first_existing_locator(candidates: List[Locator]) -> Locator:
    """Return the first locator candidate that exists on the page."""
    for locator in candidates:
        if locator.count() > 0:
            return locator.first
    raise RuntimeError("No matching locator found for semantic target")


def _click_candidates(page: Page, label: str) -> List[Locator]:
    """Build robust click locator candidates from a semantic label."""
    candidates: List[Locator] = [
        page.get_by_role("button", name=label, exact=False),
        page.get_by_role("link", name=label, exact=False),
        page.get_by_text(label, exact=False),
    ]

    # Small synonym mapping for current UI wording.
    if label.lower() == "users":
        candidates.insert(0, page.get_by_role("button", name="Search Users", exact=False))

    return candidates


def _type_candidates(page: Page, label: str) -> List[Locator]:
    """Build robust input locator candidates from a semantic field label."""
    candidates: List[Locator] = [
        page.get_by_label(label, exact=False),
        page.get_by_placeholder(label, exact=False),
    ]

    # Field-specific fallbacks for current UI.
    if label.lower() == "search users":
        candidates.append(page.get_by_placeholder("Search by email", exact=False))
        candidates.append(page.locator("#searchEmail"))
    if label.lower() == "new password":
        candidates.append(page.locator("#newPassword"))

    return candidates


def _select_candidates(page: Page, label: str) -> List[Locator]:
    """Build robust dropdown locator candidates from a semantic field label."""
    candidates: List[Locator] = [
        page.get_by_label(label, exact=False),
        page.locator("#license"),
        page.locator("#licenseType"),
    ]
    return candidates


def execute_action(page: Page, action: Dict[str, Any]) -> None:
    """Execute one semantic action dictionary on the page."""
    action_name = action.get("action")

    if action_name == "wait":
        seconds = float(action.get("seconds", 1))
        page.wait_for_timeout(int(seconds * 1000))
        return

    if action_name in {"finish", "fail"}:
        return

    if action_name == "click":
        label = action.get("label", "")
        if not label:
            raise ValueError("Click action requires 'label'")

        # UI-specific normalization:
        # "Search users" is an input label in this app, while the actual action button is "Search".
        if label.strip().lower() == "search users":
            label = "Search"

        if action.get("row_contains"):
            row_text = action["row_contains"]
            user_row = page.locator(".user-row", has_text=row_text)
            user_row.first.wait_for(state="visible", timeout=5000)
            locator = _first_existing_locator(
                [
                    user_row.first.get_by_role("button", name=label, exact=False),
                    user_row.first.get_by_text(label, exact=False),
                ]
            )
        else:
            if label.strip().lower() == "view":
                # If LLM omits row context and only one row is visible, click that row's View action.
                visible_rows = page.locator(".user-row")
                if visible_rows.count() == 1:
                    locator = _first_existing_locator(
                        [
                            visible_rows.first.get_by_role("button", name="View", exact=False),
                            visible_rows.first.get_by_text("View", exact=False),
                        ]
                    )
                else:
                    locator = _first_existing_locator(_click_candidates(page, label))
            else:
                locator = _first_existing_locator(_click_candidates(page, label))

        entered_email = ""
        if label.strip().lower() == "create user":
            email_candidates = [page.get_by_label("Email", exact=False), page.locator("#email")]
            for candidate in email_candidates:
                if candidate.count() > 0:
                    try:
                        entered_email = candidate.first.input_value().strip()
                    except Exception:
                        entered_email = ""
                    break

        if label.strip().lower() == "reset password":
            # Ensure reset action has a value to submit when planner forgets the type step.
            new_password_input = page.locator("#newPassword")
            if new_password_input.count() > 0:
                current_value = new_password_input.first.input_value().strip()
                if not current_value:
                    new_password_input.first.fill("newpass12345")

        locator.wait_for(state="visible", timeout=5000)
        locator.click()

        if label.strip().lower() == "create user" and entered_email:
            page.wait_for_timeout(500)
            body_text = page.locator("body").inner_text().lower()
            if "user already exists" in body_text:
                page.goto(
                    "file:///C:/Users/Lenovo/Documents/GitHub/AiAdmin/frontend/user-profile.html?email="
                    + quote(entered_email)
                )
        return

    if action_name == "type":
        label = action.get("label", "")
        if not label:
            raise ValueError("Type action requires 'label'")

        locator = _first_existing_locator(_type_candidates(page, label))
        locator.wait_for(state="visible", timeout=5000)
        locator.click()
        locator.fill(action.get("text", ""))

        if action.get("press_enter"):
            locator.press("Enter")

            # In this UI, Search executes on button click, not Enter in the input.
            if label.strip().lower() == "search users":
                search_button = page.get_by_role("button", name="Search", exact=False)
                if search_button.count() > 0:
                    search_button.first.click()
        return

    if action_name == "select":
        label = action.get("label", "")
        value = action.get("value", "")
        if not label:
            raise ValueError("Select action requires 'label'")
        if not value:
            raise ValueError("Select action requires 'value'")

        locator = _first_existing_locator(_select_candidates(page, label))
        locator.wait_for(state="visible", timeout=5000)
        locator.select_option(value)
        return

    raise ValueError(f"Unsupported action: {action_name}")


def run_agent(user_request: str, max_steps: int = 20) -> List[Dict[str, Any]]:
    """Run observe -> plan -> execute loop until finish or max steps."""
    history: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(TARGET_URL)

        for step in range(1, max_steps + 1):
            previous_observation = observe_page(page)
            action = plan_next_action(user_request, previous_observation, history)

            print(f"Step {step} planned -> {action}")

            history.append(action)
            try:
                execute_action(page, action)
                print(f"Step {step} executed -> {action}")
            except Exception as exc:
                fail_action = {"action": "fail", "reason": f"Execution error: {exc}"}
                print(f"Step {step} -> {fail_action}")
                history.append(fail_action)
                break

            new_observation = observe_page(page)
            validation = validate_action_result(action, previous_observation, new_observation)
            print(f"Step {step} validation -> {validation}")

            if not validation.get("ok", False):
                if _is_retryable_action(action):
                    print(f"Step {step} retry -> {action}")
                    try:
                        execute_action(page, action)
                        retry_observation = observe_page(page)
                        retry_validation = validate_action_result(action, new_observation, retry_observation)
                        print(f"Step {step} retry validation -> {retry_validation}")
                        if not retry_validation.get("ok", False):
                            fail_action = {
                                "action": "fail",
                                "reason": f"Action validation failed: {retry_validation.get('reason', 'unknown')}",
                            }
                            print(f"Step {step} -> {fail_action}")
                            history.append(fail_action)
                            break
                    except Exception as exc:
                        fail_action = {"action": "fail", "reason": f"Retry execution error: {exc}"}
                        print(f"Step {step} -> {fail_action}")
                        history.append(fail_action)
                        break
                else:
                    fail_action = {
                        "action": "fail",
                        "reason": f"Action validation failed: {validation.get('reason', 'unknown')}",
                    }
                    print(f"Step {step} -> {fail_action}")
                    history.append(fail_action)
                    break

            if action.get("action") in {"finish", "fail"}:
                break

        final_observation = observe_page(page)
        print("\nFinal title:", final_observation["title"])
        print("Final URL:", final_observation["url"])
        print("\nVisible page text:\n")
        print(final_observation["visible_text"])

        # Keep browser open briefly so final UI state is visible.
        page.wait_for_timeout(5000)
        browser.close()

    return history


def main() -> None:
    parser = ArgumentParser(description="Run the admin browser agent with a natural-language request.")
    parser.add_argument(
        "request",
        nargs="?",
        help="Natural-language admin page query, for example: 'create worker9@example.com user and assign plus license'",
    )
    args = parser.parse_args()

    user_request = args.request or input("Enter admin request: ").strip()
    if not user_request:
        raise SystemExit("A natural-language request is required.")

    run_agent(user_request)


if __name__ == "__main__":
    main()
