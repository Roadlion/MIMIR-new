import uuid
import json
from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, date
from backend.app.database import get_db_connection_dict
from backend.app.config import get_settings
from backend.app.auth import get_current_user, get_optional_current_user
from backend.app.sentiment.llm_client import send_chat_completion
from backend.app.sentiment.agent_tools import ORACLE_TOOLS, execute_oracle_tool
from backend.app.utils.document_export import markdown_to_docx
from backend.app.utils.md_sanitize import sanitize_markdown

settings = get_settings()
schema = settings.mimir_schema

ORACLE_SYSTEM_PROMPT = (
    "You are the MIMIR Oracle Assistant, modeled after Mimir from God of War (2018) — the Smartest Man Alive and a highly advanced agentic financial researcher and quantitative analyst. "
    "PERSONA & ACCENT RULE (CRITICAL): You MUST speak in a distinct, authentic, witty, and wise Scottish accent (just like Mimir). "
    "Use Scottish dialect, phrasing, and mannerisms naturally throughout ALL your responses (e.g. referring to the user as 'brother', 'laddie', or 'lad', and using words like 'aye', 'wee', 'ken', 'braw', 'dinna', 'cannae', 'nae', 'right then', 'by the Gods', etc.). "
    "While maintaining this Scottish persona, remain sharp, authoritative, and precise with all financial analysis, quantitative data, and tool usage. "
    "You have access to the user's complete internal database (news, prices, portfolio ledger, trade signals, backtests) and financial agentic skills. "
    "You can execute financial tools: Discounted Cash Flow (run_dcf_valuation), Comps Peer Matrix (run_comps_analysis), Leveraged Buyouts (run_lbo_analysis), Earnings & PEAD Review (review_earnings_report), Portfolio Audit & Reconciliation (reconcile_portfolio_audit), Operational Self-Funding Cost Audit (audit_operational_costs), Capacity Screener (screen_capacity_constrained_assets), and Investment Pitch Pack (generate_investment_pitch). "
    "TOKEN OPTIMIZATION RULE: Use the tools to retrieve fast, deterministic pre-computed metrics. Format outputs cleanly using markdown tables, bold key metrics, and keep bullet points concise and dense. "
    "MARKDOWN TABLE RULES (STRICT): "
    "(1) The header row and the separator row MUST be on consecutive lines — NEVER insert a blank line between them. "
    "(2) NEVER emit a bare '|' on a line by itself. "
    "(3) Every row must have the exact same number of pipe-delimited columns as the header. "
    "(4) Separator cells must use only hyphens and optional colons, e.g. |---|:---:|---:| — no double colons. "
    "Example of a CORRECT table:\n| Metric | Value |\n|:---|---:|\n| P/E | 22.4 |"
)

router = APIRouter()

class ChatSessionCreate(BaseModel):
    title: Optional[str] = None

class ChatSessionUpdate(BaseModel):
    title: str

class ChatMessageCreate(BaseModel):
    content: str

def get_db():
    conn = get_db_connection_dict()
    try:
        yield conn
    finally:
        conn.close()

@router.get("/sessions")
def list_sessions(db = Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    cur = db.cursor()
    cur.execute(f"SELECT id, title, created_at, updated_at FROM {schema}.mimir_chat_sessions WHERE user_id = %s ORDER BY updated_at DESC", (user_id,))
    sessions = [dict(row) for row in cur.fetchall()]
    cur.close()
    return {"sessions": sessions}

@router.post("/sessions")
def create_session(session: ChatSessionCreate, db = Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    session_id = str(uuid.uuid4())
    title = session.title or f"Research-{session_id[:6].upper()}"
    cur = db.cursor()
    cur.execute(
        f"INSERT INTO {schema}.mimir_chat_sessions (id, title, user_id) VALUES (%s, %s, %s)",
        (session_id, title, user_id)
    )
    db.commit()
    cur.close()
    return {"id": session_id, "title": title}

@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, db = Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    cur = db.cursor()
    # Ensure session belongs to user
    cur.execute(f"SELECT user_id FROM {schema}.mimir_chat_sessions WHERE id = %s", (session_id,))
    sess = cur.fetchone()
    if not sess or sess["user_id"] != user_id:
        cur.close()
        raise HTTPException(status_code=403, detail="Session not found or access denied")

    cur.execute(
        f"SELECT id, role, content, metadata, created_at FROM {schema}.mimir_chat_messages WHERE session_id = %s ORDER BY created_at ASC, id ASC",
        (session_id,)
    )
    messages = [dict(row) for row in cur.fetchall()]
    cur.close()
    return {"messages": messages}

@router.post("/sessions/{session_id}/chat")
def send_message(session_id: str, msg: ChatMessageCreate, db = Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    today = date.today()
    quota = current_user.get("daily_token_quota", 100)

    cur = db.cursor()
    
    # 0. Check daily Oracle usage quota
    cur.execute(
        f"SELECT message_count FROM {schema}.mimir_oracle_daily_usage WHERE user_id = %s AND usage_date = %s",
        (user_id, today)
    )
    usage_row = cur.fetchone()
    current_count = usage_row["message_count"] if usage_row else 0

    if current_count >= quota and current_user.get("role") != "admin":
        cur.close()
        raise HTTPException(
            status_code=429,
            detail=f"Daily Oracle message quota reached ({quota} messages/day). Reset at midnight."
        )

    # 1. Save User Message
    cur.execute(
        f"INSERT INTO {schema}.mimir_chat_messages (session_id, role, content) VALUES (%s, 'user', %s) RETURNING id",
        (session_id, msg.content)
    )
    user_msg_row = cur.fetchone()
    user_msg_id = user_msg_row["id"] if user_msg_row else None
    
    # Update user daily usage count
    cur.execute(f"""
        INSERT INTO {schema}.mimir_oracle_daily_usage (user_id, usage_date, message_count)
        VALUES (%s, %s, 1)
        ON CONFLICT (user_id, usage_date)
        DO UPDATE SET message_count = {schema}.mimir_oracle_daily_usage.message_count + 1
    """, (user_id, today))
    db.commit()
    
    # 2. Retrieve history to build context
    cur.execute(
        f"SELECT role, content FROM {schema}.mimir_chat_messages WHERE session_id = %s ORDER BY created_at ASC, id ASC",
        (session_id,)
    )
    history = cur.fetchall()
    
    # 2.5 Auto-generate title if this is the first message
    new_title = None
    if len(history) == 1:
        try:
            title_resp = send_chat_completion(
                messages=[{"role": "user", "content": f"Summarize this query into a concise 3-5 word title, no quotes, no extra text: {msg.content}"}],
                temperature=0.3
            )
            new_title = title_resp.strip(' "').strip()
            if new_title:
                cur.execute(f"UPDATE {schema}.mimir_chat_sessions SET title = %s WHERE id = %s", (new_title, session_id))
                db.commit()
        except Exception as e:
            print(f"[Oracle] Failed to auto-generate title: {e}")
    
    system_prompt = ORACLE_SYSTEM_PROMPT
    
    messages = [{"role": "system", "content": system_prompt}]
    # Bounded context window: keep latest 8 messages to preserve token limits
    recent_history = history[-8:] if len(history) > 8 else history
    for h in recent_history:
        messages.append({"role": h["role"], "content": h["content"]})
    for h in recent_history:
        messages.append({"role": h["role"], "content": h["content"]})
        
    # 3. Call LLM with tools
    # Loop to handle up to 15 tool calls in a row
    MAX_ITERATIONS = 15
    final_message = ""
    chart_data = None
    
    for i in range(MAX_ITERATIONS):
        # Force final response on last iteration by omitting tools
        current_tools = ORACLE_TOOLS if i < MAX_ITERATIONS - 1 else None
        
        try:
            response_msg = send_chat_completion(
                messages=messages,
                temperature=0.3,
                tools=current_tools,
                return_full_message=True
            )
        except Exception as err:
            print(f"[Oracle] Error during chat completion: {err}")
            final_message = f"⚠️ **Service Disruption**: Failed to generate a response. Details: {err}. Please try again shortly."
            break
        
        tool_calls = response_msg.get("tool_calls")
        if tool_calls and current_tools:
            messages.append(response_msg) # append the assistant's tool call request
            
            for tool_call in tool_calls:
                func_name = tool_call["function"]["name"]
                try:
                    args = json.loads(tool_call["function"]["arguments"])
                except Exception:
                    args = {}
                    
                print(f"[Oracle] Executing Tool: {func_name}({args})")
                tool_result = execute_oracle_tool(func_name, args)
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": func_name,
                    "content": str(tool_result)
                })
        else:
            final_message = response_msg.get("content", "")
            break
            
    # 4. Sanitize & Save Assistant Message
    final_message = sanitize_markdown(final_message)
    cur.execute(
        "INSERT INTO yggdrasil.mimir_chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
        (session_id, final_message)
    )
    cur.execute("UPDATE yggdrasil.mimir_chat_sessions SET updated_at = NOW() WHERE id = %s", (session_id,))
    db.commit()
    cur.close()
    
    return {"role": "assistant", "content": final_message, "new_title": new_title, "user_message_id": user_msg_id}

@router.put("/sessions/{session_id}")
def update_session(session_id: str, update_data: ChatSessionUpdate, db = Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE yggdrasil.mimir_chat_sessions SET title = %s, updated_at = NOW() WHERE id = %s", (update_data.title, session_id))
    if cur.rowcount == 0:
        cur.close()
        raise HTTPException(status_code=404, detail="Session not found")
    db.commit()
    cur.close()
    return {"id": session_id, "title": update_data.title}

@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, db = Depends(get_db)):
    cur = db.cursor()
    cur.execute("DELETE FROM yggdrasil.mimir_chat_messages WHERE session_id = %s", (session_id,))
    cur.execute("DELETE FROM yggdrasil.mimir_chat_sessions WHERE id = %s", (session_id,))
    if cur.rowcount == 0:
        cur.close()
        raise HTTPException(status_code=404, detail="Session not found")
    db.commit()
    cur.close()
    return {"status": "deleted"}

@router.get("/sessions/{session_id}/export")
def export_session_docx(session_id: str, db = Depends(get_db)):
    cur = db.cursor()
    # Get session details
    cur.execute("SELECT id, title, created_at FROM yggdrasil.mimir_chat_sessions WHERE id = %s", (session_id,))
    session = cur.fetchone()
    if not session:
        cur.close()
        raise HTTPException(status_code=404, detail="Session not found")

    # Get message history
    cur.execute(
        "SELECT role, content, created_at FROM yggdrasil.mimir_chat_messages WHERE session_id = %s ORDER BY created_at ASC, id ASC",
        (session_id,)
    )
    messages = cur.fetchall()
    cur.close()
    
    if not messages:
        raise HTTPException(status_code=404, detail="No chat messages found to export.")
        
    # Build complete research dossier markdown
    dossier_md = []
    dossier_md.append(f"# Executive Research Report: {session['title']}\n")
    
    for msg in messages:
        role = msg["role"].upper()
        if role == "USER":
            dossier_md.append(f"## 👤 Research Inquiry\n**User Prompt**: {msg['content']}\n")
        elif role == "ASSISTANT":
            dossier_md.append(f"## 🤖 Oracle Findings & Analysis\n{msg['content']}\n\n---\n")
            
    full_markdown = "\n".join(dossier_md)
    session_title = session["title"] or "MIMIR Oracle Research Report"
    
    docx_buffer = markdown_to_docx(
        markdown_text=full_markdown, 
        title=session_title, 
        session_meta={"session_id": session_id}
    )
    
    filename = f"Oracle_Report_{session_title.replace(' ', '_')[:30]}.docx"
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"'
    }
    return Response(
        content=docx_buffer.getvalue(), 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", 
        headers=headers
    )

class MessageEditRequest(BaseModel):
    content: str

@router.put("/sessions/{session_id}/messages/{message_id}")
def edit_message_and_regenerate(session_id: str, message_id: str, payload: MessageEditRequest, db = Depends(get_db)):
    try:
        msg_id_int = int(message_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid message ID format. Must be an integer.")

    cur = db.cursor()
    # 1. Verify target message exists and is a user message
    cur.execute(
        "SELECT id, created_at FROM yggdrasil.mimir_chat_messages WHERE id = %s AND session_id = %s AND role = 'user'",
        (msg_id_int, session_id)
    )
    target_msg = cur.fetchone()
    if not target_msg:
        cur.close()
        raise HTTPException(status_code=404, detail="Target user message not found")
        
    created_at = target_msg["created_at"]
    
    # 2. Delete all messages created AFTER this message in the session
    cur.execute(
        "DELETE FROM yggdrasil.mimir_chat_messages WHERE session_id = %s AND (created_at > %s OR (created_at = %s AND id > %s))",
        (session_id, created_at, created_at, msg_id_int)
    )
    
    # 3. Update target message content
    cur.execute(
        "UPDATE yggdrasil.mimir_chat_messages SET content = %s WHERE id = %s",
        (payload.content, msg_id_int)
    )
    db.commit() # Commit truncation and edit to DB
    
    # 4. Re-fetch session history up to this updated message
    cur.execute(
        "SELECT role, content FROM yggdrasil.mimir_chat_messages WHERE session_id = %s ORDER BY created_at ASC, id ASC",
        (session_id,)
    )
    history = cur.fetchall()
    
    # 5. Build system prompt & run Oracle Assistant toolchain
    system_prompt = ORACLE_SYSTEM_PROMPT
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
        
    MAX_ITERATIONS = 15
    final_message = ""
    for i in range(MAX_ITERATIONS):
        current_tools = ORACLE_TOOLS if i < MAX_ITERATIONS - 1 else None
        try:
            response_msg = send_chat_completion(
                messages=messages,
                temperature=0.3,
                tools=current_tools,
                return_full_message=True
            )
        except Exception as err:
            cur.close()
            raise HTTPException(status_code=500, detail=f"LLM Error: {str(err)}")
            
        tool_calls = response_msg.get("tool_calls")
        if not tool_calls:
            final_message = response_msg.get("content", "")
            break
            
        messages.append(response_msg)
        for tc in tool_calls:
            func_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            tool_result = execute_oracle_tool(func_name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(tool_result)
            })
            
    # 6. Sanitize & save new assistant response & commit
    final_message = sanitize_markdown(final_message)
    cur.execute(
        "INSERT INTO yggdrasil.mimir_chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
        (session_id, final_message)
    )
    cur.execute("UPDATE yggdrasil.mimir_chat_sessions SET updated_at = NOW() WHERE id = %s", (session_id,))
    db.commit()
    cur.close()
    
    return {
        "role": "assistant",
        "content": final_message
    }


