import uuid
import json
from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from backend.app.database import get_db_connection_dict
from backend.app.sentiment.llm_client import send_chat_completion
from backend.app.sentiment.agent_tools import ORACLE_TOOLS, execute_oracle_tool
from backend.app.utils.document_export import markdown_to_docx

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
def list_sessions(db = Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, title, created_at, updated_at FROM yggdrasil.mimir_chat_sessions ORDER BY updated_at DESC")
    sessions = [dict(row) for row in cur.fetchall()]
    cur.close()
    return {"sessions": sessions}

@router.post("/sessions")
def create_session(session: ChatSessionCreate, db = Depends(get_db)):
    session_id = str(uuid.uuid4())
    title = session.title or f"Research-{session_id[:6].upper()}"
    cur = db.cursor()
    cur.execute(
        "INSERT INTO yggdrasil.mimir_chat_sessions (id, title) VALUES (%s, %s)",
        (session_id, title)
    )
    db.commit()
    cur.close()
    return {"id": session_id, "title": title}

@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, db = Depends(get_db)):
    cur = db.cursor()
    cur.execute(
        "SELECT id, role, content, metadata, created_at FROM yggdrasil.mimir_chat_messages WHERE session_id = %s ORDER BY created_at ASC",
        (session_id,)
    )
    messages = [dict(row) for row in cur.fetchall()]
    cur.close()
    return {"messages": messages}

@router.post("/sessions/{session_id}/chat")
def send_message(session_id: str, msg: ChatMessageCreate, db = Depends(get_db)):
    # 1. Save User Message
    cur = db.cursor()
    cur.execute(
        "INSERT INTO yggdrasil.mimir_chat_messages (session_id, role, content) VALUES (%s, 'user', %s)",
        (session_id, msg.content)
    )
    
    # 2. Retrieve history to build context
    cur.execute(
        "SELECT role, content FROM yggdrasil.mimir_chat_messages WHERE session_id = %s ORDER BY created_at ASC",
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
                cur.execute("UPDATE yggdrasil.mimir_chat_sessions SET title = %s WHERE id = %s", (new_title, session_id))
        except Exception as e:
            print(f"[Oracle] Failed to auto-generate title: {e}")
    
    system_prompt = (
        "You are the MIMIR Oracle Assistant, a highly advanced financial researcher. "
        "You have access to the user's internal database (news, prices, portfolio) and the web. "
        "Use your tools to find accurate data. When returning charts, output the data clearly in markdown tables or bullet points."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
        
    # 3. Call LLM with tools
    # Loop to handle up to 15 tool calls in a row
    MAX_ITERATIONS = 15
    final_message = ""
    chart_data = None
    
    for i in range(MAX_ITERATIONS):
        # Force final response on last iteration by omitting tools
        current_tools = ORACLE_TOOLS if i < MAX_ITERATIONS - 1 else None
        
        response_msg = send_chat_completion(
            messages=messages,
            temperature=0.3,
            tools=current_tools,
            return_full_message=True
        )
        
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
            
    # 4. Save Assistant Message
    cur.execute(
        "INSERT INTO yggdrasil.mimir_chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
        (session_id, final_message)
    )
    cur.execute("UPDATE yggdrasil.mimir_chat_sessions SET updated_at = NOW() WHERE id = %s", (session_id,))
    db.commit()
    cur.close()
    
    return {"role": "assistant", "content": final_message, "new_title": new_title}

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
    cur.execute(
        "SELECT content FROM yggdrasil.mimir_chat_messages WHERE session_id = %s AND role = 'assistant' ORDER BY created_at DESC LIMIT 1",
        (session_id,)
    )
    msg = cur.fetchone()
    cur.close()
    
    if not msg:
        raise HTTPException(status_code=404, detail="No assistant messages found to export.")
        
    docx_buffer = markdown_to_docx(msg["content"])
    
    headers = {
        'Content-Disposition': f'attachment; filename="Oracle_Report_{session_id[:8]}.docx"'
    }
    return Response(
        content=docx_buffer.getvalue(), 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", 
        headers=headers
    )
