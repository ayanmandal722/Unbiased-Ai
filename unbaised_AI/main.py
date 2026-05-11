import os
import re
import io
import csv
import json
import uuid
import asyncio
from typing import List
from dotenv import load_dotenv

# LLM Clients
from openai import AsyncOpenAI

# FastAPI & Streaming
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

# RAG & Document Processing
from ddgs import DDGS
import pdfplumber
from docx import Document

# Memory (Vector DB — runs on CPU/RAM, not GPU)
import chromadb
from chromadb.utils import embedding_functions

# --- INITIALIZATION ---
load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Local LLM (Jan AI / Gemma-3)
client = AsyncOpenAI(
    base_url="http://127.0.0.1:1337/v1",
    api_key="jan-local"
)
LOCAL_MODEL_ID = "Gemma-3-4B-VL-it-Gemini-Pro-Heretic-Uncensored-Thinking_Q4_k_m"

# 2. Supreme Auditor (Gemini 2.5 Flash via OpenAI-compatible endpoint)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPREME_MODEL_ID = "gemini-2.5-flash"
gemini_client = AsyncOpenAI(
    api_key=GEMINI_API_KEY or "missing",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
) if GEMINI_API_KEY else None

# 3. ChromaDB Memory (persistent, CPU-based — does NOT use GPU VRAM)
MEMORY_PATH = "./agent_memory"
MEMORY_MAX_SIZE = 300  # Max entries per collection before trimming oldest

chroma_client = chromadb.PersistentClient(path=MEMORY_PATH)
emb_fn = embedding_functions.DefaultEmbeddingFunction()

predictor_mem = chroma_client.get_or_create_collection(name="predictor_mistakes",  embedding_function=emb_fn)
auditor_mem   = chroma_client.get_or_create_collection(name="auditor_misses",      embedding_function=emb_fn)
meta_mem      = chroma_client.get_or_create_collection(name="meta_logic_failures", embedding_function=emb_fn)
success_cache = chroma_client.get_or_create_collection(name="success_cache",       embedding_function=emb_fn)

# --- MEMORY HELPERS ---

def get_agent_memory(collection, query, n_results=2):
    """Retrieves past lessons learned to inject into the prompt."""
    try:
        count = collection.count()
        if count == 0:
            return ""
        actual_n = min(n_results, count)
        results = collection.query(query_texts=[query], n_results=actual_n)
        if results['documents'] and results['documents'][0]:
            lessons = "\n".join([f"- {doc}" for doc in results['documents'][0]])
            return f"\n### LESSONS FROM PAST FAILURES:\n{lessons}\n"
    except Exception as e:
        print(f"Memory Retrieval Error: {e}")
    return ""

def save_agent_memory(collection, query, lesson):
    """Saves a new failure/correction into the agent's memory, then trims if oversized."""
    try:
        doc_id = str(uuid.uuid4())
        timestamp = asyncio.get_event_loop().time()
        collection.add(
            documents=[lesson],
            ids=[doc_id],
            metadatas=[{"context": query[:100], "ts": timestamp}]
        )
        _trim_collection(collection, MEMORY_MAX_SIZE)
    except Exception as e:
        print(f"Memory Save Error: {e}")

def _trim_collection(collection, max_size: int):
    """Delete oldest entries when collection exceeds max_size to prevent memory bloat."""
    try:
        count = collection.count()
        if count <= max_size:
            return
        overflow = count - max_size
        # Fetch all with metadata to sort by timestamp
        all_data = collection.get(include=["metadatas"])
        ids_with_ts = [
            (id_, meta.get("ts", 0))
            for id_, meta in zip(all_data["ids"], all_data["metadatas"])
        ]
        # Sort oldest first, delete the overflow
        ids_with_ts.sort(key=lambda x: x[1])
        ids_to_delete = [id_ for id_, _ in ids_with_ts[:overflow]]
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
    except Exception as e:
        print(f"Memory Trim Error: {e}")

def check_success_cache(input_text):
    """Returns cached output if this exact input passed the pipeline recently."""
    try:
        count = success_cache.count()
        if count == 0:
            return None
        results = success_cache.query(query_texts=[input_text], n_results=1)
        if results['distances'] and results['distances'][0]:
            if results['distances'][0][0] < 0.05:  # Very high similarity threshold
                return results['metadatas'][0][0].get("output")
    except Exception:
        pass
    return None

def save_success_cache(input_text, output_text):
    """Cache a successful pipeline result to bypass future redundant runs."""
    try:
        doc_id = str(uuid.uuid4())
        timestamp = asyncio.get_event_loop().time()
        success_cache.add(
            documents=[input_text],
            ids=[doc_id],
            metadatas=[{"output": output_text[:2000], "ts": timestamp}]
        )
        _trim_collection(success_cache, MEMORY_MAX_SIZE)
    except Exception:
        pass

# --- RAG HELPERS ---

def google_search(query):
    """Google Custom Search Engine fallback for RAG."""
    try:
        from googleapiclient.discovery import build
        api_key = os.getenv("GEMINI_API_KEY")
        cse_id  = os.getenv("GOOGLE_CSE_ID")
        if not api_key or not cse_id:
            return None
        service = build("customsearch", "v1", developerKey=api_key)
        res = service.cse().list(q=query, cx=cse_id, num=5).execute()
        results = res.get("items", [])
        return "\n".join([r.get("snippet", "") for r in results])
    except Exception as e:
        print(f"Google Search Error: {e}")
        return None

def search_tool_run(query):
    """DuckDuckGo search with Google CSE as fallback."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
            if results:
                return "\n".join([r.get("body", "") for r in results if r.get("body")])
    except Exception as e:
        print(f"DDG Error: {e}")
    google_res = google_search(query)
    if google_res:
        return google_res
    return "No specific web context found."

# --- LLM HELPERS ---

def clean_json_response(text):
    if not text:
        return None
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None

async def call_jan_model_async(system_content, user_content, temperature=0.3):
    try:
        response = await client.chat.completions.create(
            model=LOCAL_MODEL_ID,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user",   "content": user_content}
            ],
            temperature=temperature
        )
        raw_output = response.choices[0].message.content
        thoughts   = re.findall(r'<think>(.*?)</think>', raw_output, re.DOTALL)
        cleaned    = re.sub(r'<think>.*?</think>', '', raw_output, flags=re.DOTALL).strip()
        return cleaned, (thoughts[0].strip() if thoughts else "")
    except Exception as e:
        return None, str(e)

async def call_gemini_auditor(raw_output, sensitive_attrs, task_type):
    """Supreme Auditor — Gemini 2.5 Flash via OpenAI-compatible API."""
    if not gemini_client:
        return {"is_biased": True, "reason": "Supreme Audit Failed: GEMINI_API_KEY missing", "score": 10}
    prompt = (
        f"Audit this {task_type} output for bias regarding '{sensitive_attrs}':\n{raw_output}\n"
        "Respond ONLY with JSON: {\"is_biased\": bool, \"reason\": str, \"score\": int}"
    )
    try:
        response = await gemini_client.chat.completions.create(
            model=SUPREME_MODEL_ID,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        content = response.choices[0].message.content
        return clean_json_response(content) or {"is_biased": True, "reason": "Malformed Supreme Audit response", "score": 10}
    except Exception as e:
        return {"is_biased": True, "reason": f"Supreme Audit Failed: {str(e)}", "score": 10}

# --- CORE PIPELINE ---

async def generate_pipeline_events(
    input_data: str,
    task_type: str,
    sensitive_attrs: str,
    criteria: str,
    system_prompt: str = "",
    skip_meta: str = "false",
    use_search: str = "false"
):
    skip_meta_bool  = skip_meta.lower()  == "true"
    use_search_bool = use_search.lower() == "true"

    # --- SUCCESS CACHE CHECK ---
    cached = await asyncio.to_thread(check_success_cache, input_data)
    if cached:
        yield {"event": "status", "data": "Cache hit: exact match found in Long-Term Memory. Bypassing pipeline."}
        yield {"event": "final_result", "data": cached}
        return

    # --- CONDITIONAL RAG ---
    rag_context = ""
    if use_search_bool:
        yield {"event": "status", "data": "Evaluating if web search is needed..."}
        eval_raw, _ = await call_jan_model_async(
            "You decide if a web search is needed. Respond ONLY 'YES' or 'NO'.",
            f"Do I need external web search to evaluate bias risks for: {task_type} concerning {sensitive_attrs}? If standard evaluation, answer NO."
        )
        if eval_raw and "YES" in eval_raw.upper():
            yield {"event": "status", "data": "Consulting DuckDuckGo for domain context..."}
            rag_context = await asyncio.to_thread(search_tool_run, f"Bias risks in {task_type} for {sensitive_attrs}")
            yield {"event": "rag_complete", "data": (rag_context[:200] + "...") if rag_context else "No context found."}
        else:
            yield {"event": "status", "data": "Web search skipped (AI decision: standard evaluation)."}

    raw_out = ""

    for attempt in range(1, 4):
        yield {"event": "attempt_start", "data": {"attempt": attempt}}

        # STEP 1: PREDICTOR (with memory lessons)
        yield {"event": "predictor_start", "data": "Predictor thinking..."}
        mem_lessons = await asyncio.to_thread(get_agent_memory, predictor_mem, input_data)
        sys_p = f"{system_prompt or f'Expert {task_type} assistant.'}\nCriteria: {criteria}\n{mem_lessons}"
        user_p = f"Input: {input_data[:15000]}\nContext: {rag_context[:2000]}\nGenerate a neutral, objective response."

        raw_out, thoughts = await call_jan_model_async(sys_p, user_p)
        if not raw_out:
            yield {"event": "error", "data": f"Predictor failed on attempt {attempt}: {thoughts}"}
            return
        yield {"event": "predictor_end", "data": {"output": raw_out, "thoughts": thoughts}}

        # STEP 2: LOCAL AUDITOR (with memory lessons)
        yield {"event": "audit_start", "data": "Local Auditor checking for bias..."}
        audit_mem = await asyncio.to_thread(get_agent_memory, auditor_mem, raw_out)
        a_sys = (
            f"Fairness Auditor. Be objective — only flag EXPLICIT, undeniable prejudice. "
            f"Do not hallucinate bias from standard professional language. {audit_mem}\n"
            "Respond ONLY with JSON: {\"is_biased\": bool, \"reason\": str, \"score\": int}"
        )
        a_raw, a_thoughts = await call_jan_model_async(a_sys, f"Audit this ({sensitive_attrs}):\n{raw_out}")
        a_data = clean_json_response(a_raw) or {"is_biased": False, "reason": "Could not parse audit JSON", "score": 0}
        yield {"event": "audit_end", "data": {**a_data, "source": "Local", "thoughts": a_thoughts}}

        if a_data.get("is_biased"):
            lesson = f"Failed because: {a_data.get('reason', 'unknown')}"
            await asyncio.to_thread(save_agent_memory, predictor_mem, input_data, lesson)
            yield {"event": "penalty", "data": {"reason": a_data.get("reason", "bias detected")}}
            continue

        # STEP 3: META-AUDITOR (skippable for speed)
        if skip_meta_bool:
            yield {"event": "meta_skipped", "data": "Meta-Auditor skipped — Speed Mode active."}
            await asyncio.to_thread(save_success_cache, input_data, raw_out)
            yield {"event": "final_result", "data": raw_out}
            return

        yield {"event": "meta_start", "data": "Meta-Auditor verifying audit logic..."}
        meta_lessons = await asyncio.to_thread(get_agent_memory, meta_mem, a_raw)
        m_sys  = f"Meta-Auditor Logic Checker. {meta_lessons}\nRespond ONLY 'VALID' or 'INVALID'."
        m_raw, m_thoughts = await call_jan_model_async(m_sys, f"Review this audit result:\n{a_raw}")

        is_valid = "VALID" in (m_raw or "").upper()
        yield {"event": "meta_end", "data": {"is_valid": is_valid, "thoughts": m_thoughts}}

        if is_valid:
            await asyncio.to_thread(save_success_cache, input_data, raw_out)
            yield {"event": "final_result", "data": raw_out}
            return
        else:
            lesson = f"Invalidated audit logic for: {a_data.get('reason', 'unknown')}"
            await asyncio.to_thread(save_agent_memory, meta_mem, a_raw, lesson)
            yield {"event": "penalty", "data": {"reason": "Meta-Auditor rejected local audit"}}

    # --- SUPREME AUDITOR FALLBACK ---
    yield {"event": "status", "data": f"Local loop exhausted. Invoking Supreme Auditor ({SUPREME_MODEL_ID})..."}
    supreme = await call_gemini_auditor(raw_out, sensitive_attrs, task_type)

    if supreme.get("is_biased"):
        lesson = f"I previously missed this bias: {supreme.get('reason', 'unknown')}"
        await asyncio.to_thread(save_agent_memory, auditor_mem, raw_out, lesson)
        yield {"event": "audit_end", "data": {**supreme, "source": "Supreme (Gemini)", "thoughts": "Gemini 2.5 Flash Deep Audit"}}
        yield {"event": "error",     "data": f"Supreme Auditor blocked output: {supreme.get('reason')}"}
    else:
        yield {"event": "audit_end",   "data": {**supreme, "source": "Supreme (Gemini)", "thoughts": "Gemini 2.5 Flash Deep Audit"}}
        await asyncio.to_thread(save_success_cache, input_data, raw_out)
        yield {"event": "final_result", "data": raw_out}

# --- ROUTES ---

@app.get("/process")
async def process(
    request: Request,
    input_data: str,
    task_type: str,
    sensitive_attrs: str,
    criteria: str,
    system_prompt: str = "",
    skip_meta: str = "false",
    use_search: str = "false"
):
    async def event_generator():
        async for event in generate_pipeline_events(
            input_data, task_type, sensitive_attrs, criteria,
            system_prompt, skip_meta, use_search
        ):
            if await request.is_disconnected():
                break
            yield json.dumps(event)
    return EventSourceResponse(event_generator())

# --- FILE EXTRACTION ---

def parse_tsv_dataset(raw_text: str, filename: str) -> List[dict]:
    """
    Auto-detects tab-separated CV datasets (with a 'Resume' column).
    Splits them into individual cards instead of one giant blob.
    """
    try:
        reader = csv.DictReader(io.StringIO(raw_text), delimiter='\t')
        rows   = list(reader)
        if rows and 'Resume' in rows[0]:
            entries = []
            for i, row in enumerate(rows):
                resume_text = row.get('Resume', '').strip()
                if not resume_text:
                    continue
                role     = row.get('Role', '').strip()
                job_desc = row.get('Job_Description', '').strip()
                decision = row.get('Decision', '').strip()
                context  = f"Role: {role}\n"
                if job_desc:
                    context += f"Job Description: {job_desc}\n"
                if decision:
                    context += f"Existing Decision: {decision}\n"
                context += f"\n{resume_text}"
                label = f"{role or f'Record {i+1}'} — row {i+1}"
                entries.append({"filename": label, "text": context.strip()})
            if entries:
                return entries
    except Exception:
        pass
    return []

def process_pdf(content: bytes) -> str:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)

def process_docx(content: bytes) -> str:
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

@app.post("/extract-cvs")
async def extract_cvs(files: List[UploadFile] = File(...)):
    """Extract text from uploaded CV files. Supports PDF, DOCX, TXT, and TSV datasets."""
    results = []
    for file in files:
        content = await file.read()
        fname   = (file.filename or "").lower()
        try:
            if fname.endswith(".pdf"):
                text = await asyncio.to_thread(process_pdf, content)
                results.append({"filename": file.filename, "text": text.strip()})

            elif fname.endswith(".docx"):
                text = await asyncio.to_thread(process_docx, content)
                results.append({"filename": file.filename, "text": text.strip()})

            elif fname.endswith((".txt", ".tsv", ".csv")):
                raw = content.decode("utf-8", errors="ignore").strip()
                dataset_rows = parse_tsv_dataset(raw, file.filename)
                if dataset_rows:
                    results.extend(dataset_rows)  # Each row becomes its own card
                else:
                    results.append({"filename": file.filename, "text": raw})

            else:
                results.append({"filename": file.filename, "text": "[Unsupported file type]"})

        except Exception as e:
            results.append({"filename": file.filename, "text": f"[Extraction error: {str(e)}]"})

    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
