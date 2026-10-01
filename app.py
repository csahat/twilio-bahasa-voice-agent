import webrtcvad
import asyncio
import logging
import os
import json
import wave
import fitz  # PyMuPDF
import re
import time
import base64
import glob
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
from typing import List
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client
from twilio.request_validator import RequestValidator
import aiofiles
import struct
import audioop
import io
from pydub import AudioSegment

# Import dari LlamaIndex dan Pinecone
from pinecone import Pinecone
from llama_index.core import VectorStoreIndex, Settings, PromptTemplate, Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.llms.gemini import Gemini
from llama_index.embeddings.gemini import GeminiEmbedding

# Load environment variables
load_dotenv()

# --- Konfigurasi dan Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME")

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_API_KEY = os.getenv("TWILIO_API_KEY")
TWILIO_API_SECRET = os.getenv("TWILIO_API_SECRET")
TWILIO_TWIML_APP_SID = os.getenv("TWILIO_TWIML_APP_SID")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

app = FastAPI(title="Final Conversational AI App with Twilio")

# Cache query engine dan response cache
query_engine = None
rag_cache = {}  # Simple cache untuk RAG responses
rag_cache_ttl = 300  # 5 menit TTL

# Performance instrumentation
class PerformanceTimer:
    """Helper class untuk detailed performance tracking"""
    def __init__(self, name: str):
        self.name = name
        self.start_time = None
        self.checkpoints = {}
    
    def start(self):
        self.start_time = time.time()
        return self
    
    def checkpoint(self, label: str):
        if self.start_time is None:
            return
        elapsed = time.time() - self.start_time
        self.checkpoints[label] = elapsed
        return elapsed
    
    def end(self, log_results: bool = True):
        if self.start_time is None:
            return 0
        total = time.time() - self.start_time
        if log_results:
            logger.info(f"⏱️  {self.name} | Total: {total:.3f}s | " + 
                       " | ".join([f"{k}: {v:.3f}s" for k, v in self.checkpoints.items()]))
        return total

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global HTTP clients (reuse connection → faster)
tts_client = httpx.AsyncClient(timeout=30.0)
stt_client = httpx.AsyncClient(timeout=30.0)

# Global cooldown tracking untuk mencegah spam processing
last_processing_time = {}

# Setup Gemini dan Pinecone dengan optimasi untuk low-latency 
os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
Settings.llm = Gemini(
    model_name="models/gemini-2.0-flash-exp",  # Flash model untuk speed
    temperature=0.3,  # Lower temperature untuk faster generation
    max_tokens=150    # Limit output untuk response cepat
)
Settings.embed_model = GeminiEmbedding(model_name="models/embedding-001")
Settings.chunk_size = 512  # Smaller chunks untuk faster processing
Settings.chunk_overlap = 50  # Smaller overlap

index = None

async def initialize_pinecone():
    """Initialize Pinecone asynchronously"""
    global index
    try:
        def _init_pinecone():
            pc = Pinecone(api_key=PINECONE_API_KEY)
            if PINECONE_INDEX_NAME not in pc.list_indexes().names():
                logger.info(f"Index '{PINECONE_INDEX_NAME}' tidak ditemukan. Membuat index baru...")
                pc.create_index(
                    name=PINECONE_INDEX_NAME,
                    dimension=768,
                    metric="cosine",
                    spec={"serverless": {"cloud": "aws", "region": "us-east-1"}}
                )
                return pc, True
            return pc, False
        
        pc, needs_wait = await asyncio.to_thread(_init_pinecone)
        
        if needs_wait:
            await asyncio.sleep(5)
        
        pinecone_index = pc.Index(PINECONE_INDEX_NAME)
        vector_store = PineconeVectorStore(pinecone_index=pinecone_index)
        index = VectorStoreIndex.from_vector_store(vector_store=vector_store)
        logger.info(f"RAG Index '{pinecone_index.name}' berhasil diinisialisasi.")
    except Exception as e:
        logger.fatal(f"Gagal inisialisasi RAG Index. Error: {e}")

# Initialize on startup
@app.on_event("startup")
async def startup_event():
    await initialize_pinecone()
    # Start cache cleanup task
    asyncio.create_task(cleanup_rag_cache())

async def cleanup_rag_cache():
    """Cleanup expired cache entries setiap 5 menit"""
    while True:
        try:
            await asyncio.sleep(300)  # 5 menit
            now = time.time()
            expired_keys = [
                key for key, value in rag_cache.items() 
                if now - value['timestamp'] > rag_cache_ttl
            ]
            for key in expired_keys:
                del rag_cache[key]
            if expired_keys:
                logger.info(f"Cleaned up {len(expired_keys)} expired cache entries")
        except Exception as e:
            logger.error(f"Error in cache cleanup: {e}")

qa_prompt_tmpl_str = (
    "Anda adalah asisten AI customer service yang berbicara dengan pelanggan. "
    "Jawablah dengan singkat, sopan, dan mudah dipahami. "
    "Jawaban tidak boleh lebih dari dua kalimat. "
    "Fokus pada inti jawaban dan hindari penjelasan panjang. "
    "Jika informasi tidak tersedia, sampaikan dengan sopan dan jujur.\n"
    "Konteks: {context_str}\n"
    "Pertanyaan: {query_str}\n"
    "Jawaban: "
)
qa_prompt_tmpl = PromptTemplate(qa_prompt_tmpl_str)

# Fungsi PDF Processing
def clean_text(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

async def process_pdf_to_documents(pdf_content: bytes, filename: str) -> List[Document]:
    """Process PDF in thread to avoid blocking"""
    def _process_pdf():
        try:
            doc = fitz.open(stream=pdf_content, filetype="pdf")
            documents = []
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                text = page.get_text()
                if text.strip():
                    cleaned_text = clean_text(text)
                    if cleaned_text:
                        doc_obj = Document(
                            text=cleaned_text,
                            metadata={
                                "source": "uploaded_pdf",
                                "file_name": filename,
                                "page_number": page_num + 1
                            }
                        )
                        documents.append(doc_obj)
            doc.close()
            return documents
        except Exception as e:
            logger.error(f"Error processing PDF: {e}")
            raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    return await asyncio.to_thread(_process_pdf)

async def add_documents_to_pinecone(documents: List[Document], index_name: str = None):
    """Add documents to Pinecone in thread"""
    def _add_documents():
        try:
            if index_name is None:
                _index_name = PINECONE_INDEX_NAME
            else:
                _index_name = index_name
            
            node_parser = SentenceSplitter(chunk_size=1024, chunk_overlap=200)
            nodes = node_parser.get_nodes_from_documents(documents)
            embed_model = GeminiEmbedding(model_name="models/embedding-001")
            
            batch_size = 32
            pc = Pinecone(api_key=PINECONE_API_KEY)
            
            if _index_name not in pc.list_indexes().names():
                logger.info(f"Index '{_index_name}' tidak ditemukan. Membuat index baru...")
                pc.create_index(
                    name=_index_name,
                    dimension=768,
                    metric="cosine",
                    spec={"serverless": {"cloud": "aws", "region": "us-east-1"}}
                )
                time.sleep(5)  # This is in thread, so it's OK
            
            pinecone_index = pc.Index(_index_name)
            
            for i in range(0, len(nodes), batch_size):
                i_end = min(i + batch_size, len(nodes))
                nodes_batch = nodes[i:i_end]
                texts = [node.get_content() for node in nodes_batch]
                embeddings = embed_model.get_text_embedding_batch(texts)
                
                vectors_to_upsert = []
                for node, embedding in zip(nodes_batch, embeddings):
                    metadata = {
                        "text": node.get_content(),
                        "source": node.metadata.get('source', 'uploaded_pdf'),
                        "file_name": node.metadata.get('file_name', 'N/A'),
                        "page_number": node.metadata.get('page_number', 'N/A'),
                    }
                    vec = {"id": f"{node.node_id}_{int(time.time())}", "values": embedding, "metadata": metadata}
                    vectors_to_upsert.append(vec)
                
                pinecone_index.upsert(vectors=vectors_to_upsert)
            
            return len(nodes)
        except Exception as e:
            logger.error(f"Error adding documents to Pinecone: {e}")
            raise HTTPException(status_code=500, detail=f"Error adding documents to Pinecone: {str(e)}")
    
    return await asyncio.to_thread(_add_documents)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_root():
    return FileResponse("static/index.html")

@app.get("/call")
async def get_voice_call():
    return FileResponse("static/call.html")

@app.get("/twilio-call")
async def get_twilio_call():
    return FileResponse("static/twilio-call.html")

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "index_status": "active" if index is not None else "inactive",
        "index_name": PINECONE_INDEX_NAME,
        "rag_cache_size": len(rag_cache)
    }

@app.post("/clear-cache")
async def clear_cache():
    """Clear RAG cache manually"""
    global rag_cache
    cache_size = len(rag_cache)
    rag_cache.clear()
    return {
        "message": f"Cache cleared successfully",
        "entries_removed": cache_size
    }

# --- TWILIO VOICE ENDPOINTS ---

# Validate X-Twilio-Signature on webhooks so only Twilio can request TwiML.
# Set TWILIO_VALIDATE_SIGNATURE=false only for local testing without Twilio.
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "true").lower() != "false"
twilio_request_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

async def verify_twilio_signature(request: Request):
    """Reject webhook requests that are not signed by Twilio."""
    if not TWILIO_VALIDATE_SIGNATURE:
        return
    if twilio_request_validator is None:
        logger.error("TWILIO_AUTH_TOKEN not set; cannot validate webhook signature")
        raise HTTPException(status_code=500, detail="Webhook validation not configured")

    # Behind a tunnel or proxy (ngrok, load balancer) the app sees http://localhost,
    # but Twilio signed the public https URL, so rebuild it from forwarded headers.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    form = await request.form()
    signature = request.headers.get("x-twilio-signature", "")
    if not twilio_request_validator.validate(url, dict(form), signature):
        logger.warning(f"Rejected webhook with invalid Twilio signature: {request.url.path}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

@app.get("/twilio-token")
async def get_twilio_token():
    """
    Generate Twilio Access Token untuk browser client
    """
    try:
        # Check if all required Twilio credentials are available
        if not all([TWILIO_ACCOUNT_SID, TWILIO_API_KEY, TWILIO_API_SECRET, TWILIO_TWIML_APP_SID]):
            missing = []
            if not TWILIO_ACCOUNT_SID: missing.append("TWILIO_ACCOUNT_SID")
            if not TWILIO_API_KEY: missing.append("TWILIO_API_KEY")
            if not TWILIO_API_SECRET: missing.append("TWILIO_API_SECRET")
            if not TWILIO_TWIML_APP_SID: missing.append("TWILIO_TWIML_APP_SID")
            
            logger.error(f"Missing Twilio credentials: {', '.join(missing)}")
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Twilio credentials not configured",
                    "missing": missing,
                    "hint": "Please add these variables to your .env file"
                }
            )
        
        # Create access token
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant
        
        # Identity for this client (browser)
        identity = f"browser_client_{int(time.time())}"
        
        # Create token
        token = AccessToken(TWILIO_ACCOUNT_SID, TWILIO_API_KEY, TWILIO_API_SECRET, identity=identity)
        
        # Create voice grant
        voice_grant = VoiceGrant(
            outgoing_application_sid=TWILIO_TWIML_APP_SID,
            incoming_allow=True
        )
        
        token.add_grant(voice_grant)
        
        logger.info(f"Generated token for identity: {identity}")
        
        return {
            "token": token.to_jwt(),
            "identity": identity
        }
        
    except Exception as e:
        logger.error(f"Error generating token: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

@app.post("/twiml")
async def twiml_handler(request: Request):
    """
    TwiML endpoint untuk outgoing call dari browser
    Dipanggil oleh TwiML App saat browser client melakukan call
    """
    await verify_twilio_signature(request)
    logger.info("TwiML handler called for browser outgoing call")

    response = VoiceResponse()
    
    # Sambutan untuk browser caller
    response.say(
        "Halo, selamat datang. Anda akan terhubung dengan AI assistant kami.",
        language="id-ID"
    )
    
    # Connect ke WebSocket untuk streaming audio
    connect = Connect()
    stream = Stream(url=f'wss://{request.url.hostname}/twilio-ws')
    connect.append(stream)
    response.append(connect)
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/voice")
async def voice_webhook(request: Request):
    """
    Endpoint untuk incoming call dari nomor telepon ke Twilio
    Twilio akan menghubungi endpoint ini saat ada panggilan masuk dari telepon
    """
    await verify_twilio_signature(request)
    logger.info("Incoming phone call received")

    response = VoiceResponse()
    
    # Sambutan awal untuk phone caller
    response.say(
        "Halo, selamat datang di layanan AI assistant. Silakan tunggu sebentar, kami akan menghubungkan Anda.",
        language="id-ID"
    )
    
    # Connect ke WebSocket untuk streaming audio
    connect = Connect()
    stream = Stream(url=f'wss://{request.url.hostname}/twilio-ws')
    connect.append(stream)
    response.append(connect)
    
    return Response(content=str(response), media_type="application/xml")

# === GLOBAL CONTROLS (accessible dari seluruh module) ===
tts_playback_tasks: dict[str, asyncio.Task] = {}
tts_stop_flags: dict[str, asyncio.Event] = {}
last_processing_time = {}

# ===================================================================
# Async generator: synthesize audio from ElevenLabs streaming endpoint
# ===================================================================
async def synthesize_tts_elevenlabs(text: str):
    """
    Streaming TTS dari ElevenLabs dengan optimasi untuk low-latency
    """
    voice_id = "JBFqnCBsd6RMkjVDRZzb"
    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    
    # Optimized settings untuk low-latency voice assistant
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.65,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True
        },
        "optimize_streaming_latency": 4,
        "output_format": "mp3_44100_128"
    }

    # stream POST, yield raw bytes as they arrive
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", tts_url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                yield chunk

# ===================================================================
# Helper: stop TTS playback (barge-in)
# ===================================================================
async def stop_tts_playback(stream_sid: str):
    ev = tts_stop_flags.get(stream_sid)
    if ev and not ev.is_set():
        ev.set()
    # cancel task if exists
    task = tts_playback_tasks.get(stream_sid)
    if task and not task.done():
        task.cancel()
    # cleanup maps (caller may call again)
    tts_stop_flags.pop(stream_sid, None)
    tts_playback_tasks.pop(stream_sid, None)

# ===================================================================
# send_tts_to_twilio: do streaming TTS -> convert -> send mulaw chunks
# - runs as background task per streamSid
# - respects tts_stop_flags[streamSid] for immediate stop (barge-in)
# ===================================================================
async def send_tts_to_twilio(websocket: WebSocket, stream_sid: str, text: str):
    """
    Start streaming TTS to Twilio. This function schedules streaming
    as a background task (so websocket loop can continue receiving audio).
    Optimized for low-latency, natural voice assistant experience.
    """
    stop_event = asyncio.Event()
    tts_stop_flags[stream_sid] = stop_event

    async def _run_tts():
        try:
            # 📊 TTS TIMING INSTRUMENTATION
            tts_timer = PerformanceTimer(f"TTS-{stream_sid[:8]}").start()
            buffer = bytearray()
            first_chunk_sent = False
            first_audio_sent = False
            first_chunk_received = False
            total_chunks = 0
            
            # stream raw bytes from ElevenLabs (likely MP3)
            async for chunk in synthesize_tts_elevenlabs(text):
                total_chunks += 1
                if not first_chunk_received:
                    tts_timer.checkpoint("first_chunk_from_elevenlabs")
                    first_chunk_received = True
                if stop_event.is_set():
                    logger.info(f"Barge-in detected early for {stream_sid}")
                    return
                    
                buffer.extend(chunk)

                # Kurangi threshold untuk faster initial response
                # First chunk: 4000 bytes (~0.25s audio) untuk low latency
                # Subsequent chunks: 6000 bytes untuk smooth streaming
                threshold = 4000 if not first_chunk_sent else 6000
                
                if len(buffer) < threshold:
                    continue

                # Convert collected bytes (mp3) into raw PCM using pydub
                try:
                    audio = AudioSegment.from_file(io.BytesIO(buffer), format="mp3")
                    
                    # target: mono, 8000 Hz - optimized processing
                    audio = audio.set_channels(1).set_frame_rate(8000)
                    
                    # get raw PCM bytes
                    raw = audio.raw_data  # 16-bit samples
                    # convert PCM to mu-law
                    mulaw = audioop.lin2ulaw(raw, 2)
                    
                except Exception as e:
                    logger.warning(f"pydub failed to decode chunk: {e}, clearing buffer")
                    buffer.clear()
                    continue

                # stream mulaw to Twilio in 320-byte frames (40ms each)
                # Optimized: send frames dengan timing yang tepat
                frame_count = len(mulaw) // 320
                for frame_idx in range(frame_count):
                    if stop_event.is_set():
                        logger.info(f"Barge-in detected for {stream_sid}, stopping TTS stream.")
                        return
                    
                    i = frame_idx * 320
                    ch = mulaw[i:i+320]
                    payload = base64.b64encode(ch).decode("utf-8")
                    msg = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload}
                    }
                    
                    try:
                        await websocket.send_text(json.dumps(msg))
                        
                        # 📊 Log first audio time untuk monitoring latency
                        if not first_audio_sent:
                            tts_timer.checkpoint("first_audio_to_twilio")
                            first_audio_latency = tts_timer.checkpoints["first_audio_to_twilio"]
                            logger.info(f"🎵 TTS First Audio | Time: {first_audio_latency:.3f}s | "
                                      f"From ElevenLabs: {tts_timer.checkpoints['first_chunk_from_elevenlabs']:.3f}s | "
                                      f"Processing: {(first_audio_latency - tts_timer.checkpoints['first_chunk_from_elevenlabs']):.3f}s")
                            first_audio_sent = True
                            
                    except WebSocketDisconnect:
                        logger.info("Websocket disconnected while streaming TTS")
                        return
                    
                    # Adaptive delay: lebih cepat di awal untuk immediate feedback
                    if not first_chunk_sent and frame_idx < 3:
                        # First 3 frames: 15ms untuk instant start
                        await asyncio.sleep(0.015)
                    else:
                        # Subsequent frames: 30ms untuk smooth playback
                        await asyncio.sleep(0.030)
                
                first_chunk_sent = True
                buffer.clear()

            # After stream exhausted, flush any leftover buffer
            if buffer:
                try:
                    audio = AudioSegment.from_file(io.BytesIO(buffer), format="mp3")
                    audio = audio.set_channels(1).set_frame_rate(8000)
                    raw = audio.raw_data
                    mulaw = audioop.lin2ulaw(raw, 2)
                    
                    for i in range(0, len(mulaw), 320):
                        if stop_event.is_set():
                            logger.info(f"Barge-in detected for {stream_sid}, stop flush")
                            return
                        ch = mulaw[i:i+320]
                        payload = base64.b64encode(ch).decode("utf-8")
                        msg = {"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}
                        try:
                            await websocket.send_text(json.dumps(msg))
                        except WebSocketDisconnect:
                            return
                        await asyncio.sleep(0.030)
                except Exception as e:
                    logger.warning(f"Could not flush leftover TTS buffer: {e}")
                finally:
                    buffer.clear()

            # finally send mark to indicate end_of_speech (if still connected and not barged-in)
            if not stop_event.is_set():
                try:
                    await websocket.send_text(json.dumps({
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {"name": "end_of_speech"}
                    }))
                except WebSocketDisconnect:
                    logger.info("Websocket disconnected before sending mark")
            
            # 📊 TTS COMPLETION SUMMARY
            total_tts_time = tts_timer.end(log_results=False)
            logger.info(f"🎵 TTS Complete | Total: {total_tts_time:.3f}s | "
                      f"Chunks: {total_chunks} | "
                      f"Text length: {len(text)} chars")

        except asyncio.CancelledError:
            logger.info(f"TTS task cancelled for {stream_sid}")
        except Exception as e:
            logger.exception(f"TTS streaming error for {stream_sid}: {e}")
        finally:
            # cleanup
            tts_stop_flags.pop(stream_sid, None)
            tts_playback_tasks.pop(stream_sid, None)

    # start background task and store
    task = asyncio.create_task(_run_tts())
    tts_playback_tasks[stream_sid] = task
    return task

# ===================================================================
# STT/processing pipeline
# - convert ulaw -> wav, run STT, query RAG, then kick off TTS task
# - note: this function RETURNS quickly after scheduling TTS so main websocket loop continues
# ===================================================================
async def process_twilio_audio(websocket: WebSocket, stream_sid: str, audio_buffer: list):
    try:
        # 📊 START PERFORMANCE TRACKING
        timer = PerformanceTimer(f"Pipeline-{stream_sid[:8]}").start()
        
        # Kurangi cooldown untuk faster response
        now = time.time()
        last = last_processing_time.get(stream_sid, 0)
        if now - last < 0.5:
            logger.info("Skipping process due cooldown")
            return
        last_processing_time[stream_sid] = now
        
        full = b"".join(audio_buffer)
        if len(full) < 6000:
            logger.info("Too short audio, skipping")
            return
        
        timer.checkpoint("audio_prep")

        # convert µ-law -> linear PCM -> write wav file for STT
        def _write_wav(ulaw_bytes, out_path):
            lin = audioop.ulaw2lin(ulaw_bytes, 2)
            with wave.open(out_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)  # Twilio native sample rate
                wf.writeframes(lin)
            return len(lin)

        tmp_path = f"temp_twilio_{stream_sid}_{int(time.time()*1000)}.wav"
        pcm_length = await asyncio.to_thread(_write_wav, full, tmp_path)
        duration_ms = (pcm_length / 2) / 8000 * 1000
        logger.info(f"Created WAV file: {tmp_path} (duration: {duration_ms:.0f}ms)")
        
        timer.checkpoint("wav_creation")

        # Quick VAD validation (already filtered upstream)
        logger.info(f"Audio length: {len(full)} bytes, proceeding to STT (VAD already done upstream)")

        # 📊 STT TIMING
        stt_timer = PerformanceTimer(f"STT-{stream_sid[:8]}").start()
        transcribed_text = await transcribe_audio(tmp_path)
        stt_time = stt_timer.end(log_results=False)
        
        timer.checkpoint("stt_complete")
        logger.info(f"🎤 STT Result: '{transcribed_text}' | Time: {stt_time:.3f}s")

        # cleanup immediately
        await asyncio.to_thread(lambda p: os.remove(p) if os.path.exists(p) else None, tmp_path)

        if not transcribed_text or len(transcribed_text.strip()) < 3:
            # If STT failed, notify user
            await send_tts_to_twilio(websocket, stream_sid, "Maaf, saya tidak mendengar. Bisa ulangi?")
            return

        # 📊 RAG TIMING dengan detailed breakdown (Retrieval + Generation)
        rag_timer = PerformanceTimer(f"RAG-{stream_sid[:8]}").start()
        
        # Check cache first
        cache_key = transcribed_text.lower().strip()
        now = time.time()
        cache_hit = False
        
        if cache_key in rag_cache:
            cached_entry = rag_cache[cache_key]
            if now - cached_entry['timestamp'] < rag_cache_ttl:
                response_text = cached_entry['response']
                cache_hit = True
                rag_timer.checkpoint("cache_hit")
                logger.info(f"✨ RAG Cache HIT | Response: '{response_text[:50]}...'")
            else:
                # Cache expired
                del rag_cache[cache_key]
                response_text = None
        else:
            response_text = None
        
        # If not in cache, query RAG with standard method
        if response_text is None:
            if index is not None:
                try:
                    # Query RAG with standard query engine
                    rag_timer.checkpoint("setup_start")
                    query_engine = index.as_query_engine(
                        streaming=False,
                        text_qa_template=qa_prompt_tmpl,
                        similarity_top_k=3
                    )
                    rag_timer.checkpoint("setup_done")
                    
                    # Execute query (includes retrieval + generation)
                    rag_timer.checkpoint("query_start")
                    response = await asyncio.wait_for(
                        query_engine.aquery(transcribed_text),
                        timeout=5.0
                    )
                    rag_timer.checkpoint("query_done")
                    
                    response_text = response.response.strip()
                    
                    # Save to cache
                    rag_cache[cache_key] = {
                        'response': response_text,
                        'timestamp': now
                    }
                    
                    # Calculate times
                    setup_time = rag_timer.checkpoints["setup_done"] - rag_timer.checkpoints["setup_start"]
                    query_time = rag_timer.checkpoints["query_done"] - rag_timer.checkpoints["query_start"]
                    total_rag = rag_timer.checkpoints["query_done"] - rag_timer.checkpoints["setup_start"]
                    
                    # Log RAG breakdown
                    num_docs = len(response.source_nodes) if hasattr(response, 'source_nodes') else 'N/A'
                    logger.info(f"🔍 RAG Breakdown | Total: {total_rag:.3f}s | "
                              f"Setup: {setup_time:.3f}s | "
                              f"Query (Retrieval+Generation): {query_time:.3f}s | "
                              f"Docs: {num_docs}")
                    
                except asyncio.TimeoutError:
                    logger.error("⏱️ RAG query timeout after 5s!")
                    response_text = "Maaf, sistem sedang lambat. Bisa ulangi pertanyaan Anda?"
                    rag_timer.checkpoint("timeout")
                except Exception as e:
                    logger.exception(f"❌ RAG query error: {e}")
                    response_text = "Maaf, terjadi kesalahan saat mencari informasi."
                    rag_timer.checkpoint("error")
            else:
                response_text = "Maaf, sistem sedang tidak tersedia. Silakan coba lagi nanti."
                rag_timer.checkpoint("no_index")
        
        rag_total_time = rag_timer.end(log_results=False)
        timer.checkpoint("rag_complete")

        # Start streaming TTS immediately (background task)
        timer.checkpoint("tts_start")
        await send_tts_to_twilio(websocket, stream_sid, response_text)
        
        # 📊 FINAL PIPELINE SUMMARY
        total_time = timer.end(log_results=False)
        logger.info(f"⚡ PIPELINE SUMMARY | Total: {total_time:.3f}s | "
                  f"Prep: {timer.checkpoints['audio_prep']:.3f}s | "
                  f"WAV: {(timer.checkpoints['wav_creation'] - timer.checkpoints['audio_prep']):.3f}s | "
                  f"STT: {stt_time:.3f}s | "
                  f"RAG: {rag_total_time:.3f}s ({('cached' if cache_hit else 'query')}) | "
                  f"TTS: started")

    except Exception as e:
        logger.exception(f"Error in process_twilio_audio: {e}")
        # try to notify user
        try:
            await send_tts_to_twilio(websocket, stream_sid, "Maaf, terjadi kesalahan pada sistem.")
        except Exception:
            pass

# ===================================================================
# Main Twilio WebSocket handler (paste into app.py where your routes are)
# ===================================================================
@app.websocket("/twilio-ws")
async def twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio Media Stream connected")
    websocket._ping_interval = 20

    stream_sid = None
    call_sid = None

    # VAD setup - level 2 untuk balance (3 terlalu strict untuk 8kHz Twilio audio)
    vad = webrtcvad.Vad(2)  # Level 2: balance antara sensitivity & noise rejection
    sample_rate = 8000
    frame_ms = 30
    bytes_per_sample = 2
    frame_size = int(sample_rate * frame_ms / 1000) * bytes_per_sample
    speech_buffer = bytearray()
    silence_counter = 0
    speech_active = False
    
    # Track consecutive speech frames
    consecutive_speech_frames = 0
    recent_frames = []
    
    # Energy threshold untuk filter noise (lower untuk 8kHz audio dari Twilio)
    ENERGY_THRESHOLD = 300  # Turunkan dari 500 ke 300 untuk 8kHz Twilio audio

    try:
        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info("Twilio websocket disconnected")
                break
            except Exception as e:
                logger.exception(f"Error receiving ws message: {e}")
                break

            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid = data["start"]["callSid"]
                logger.info(f"Stream started: {stream_sid} / {call_sid}")

                # greet
                websocket.last_tts_time = time.time()
                await send_tts_to_twilio(websocket, stream_sid, "Halo! Saya adalah asisten AI Anda. Apa yang bisa saya bantu hari ini?")

            elif event == "media":
                payload_b64 = data["media"]["payload"]
                ulaw_chunk = base64.b64decode(payload_b64)
                pcm_chunk = audioop.ulaw2lin(ulaw_chunk, 2)
                speech_buffer.extend(pcm_chunk)
                
                # constants - optimized untuk voice assistant experience
                MIN_CONSECUTIVE_SPEECH = 8     # butuh 8 frame BERTURUT-TURUT (~240ms) - turunkan untuk sensitivity
                END_SILENCE_FRAMES = 15        # butuh diam ~450ms untuk faster response
                MIN_UTTERANCE_FRAMES = 25      # min total durasi ~750ms (turunkan dari 1050ms agar lebih responsif)
                GRACE_PERIOD = 1.5             # 1.5 detik untuk faster barge-in

                # cek apakah TTS masih aktif atau baru selesai
                now = time.time()
                tts_task = tts_playback_tasks.get(stream_sid)
                is_tts_active = tts_task and not tts_task.done()
                
                # Jika TTS aktif, perlu grace period lebih lama
                if is_tts_active or (hasattr(websocket, "last_tts_time") and now - websocket.last_tts_time < GRACE_PERIOD):
                    # Masih dalam grace period atau TTS masih jalan
                    # Hanya stop TTS jika benar-benar ada speech yang kuat
                    pass
                else:
                    # Reset grace period flag jika sudah cukup lama
                    if hasattr(websocket, "last_tts_time") and now - websocket.last_tts_time >= GRACE_PERIOD:
                        delattr(websocket, "last_tts_time")

                while len(speech_buffer) >= frame_size:
                    frame = bytes(speech_buffer[:frame_size])
                    del speech_buffer[:frame_size]

                    # Hitung energy level dari frame
                    try:
                        rms = audioop.rms(frame, 2)  # Root Mean Square untuk energy
                    except:
                        rms = 0
                    
                    # Cek VAD dan energy threshold
                    is_voice = False
                    try:
                        is_voice = vad.is_speech(frame, sample_rate) and rms > ENERGY_THRESHOLD
                    except:
                        is_voice = False

                    if is_voice:
                        silence_counter = 0
                        consecutive_speech_frames += 1
                        recent_frames.append(frame)

                        # Hanya aktifkan speech detection jika:
                        # 1. Ada MIN_CONSECUTIVE_SPEECH frames BERTURUT-TURUT
                        # 2. Energy cukup tinggi (human speech)
                        if not speech_active and consecutive_speech_frames >= MIN_CONSECUTIVE_SPEECH:
                            # Double check: pastikan TTS memang sedang jalan untuk barge-in
                            tts_task = tts_playback_tasks.get(stream_sid)
                            if tts_task and not tts_task.done():
                                speech_active = True
                                logger.info(f"🎤 Human speech detected (consecutive: {consecutive_speech_frames}, energy: {rms}) -> BARGE-IN")
                                await stop_tts_playback(stream_sid)
                                websocket.last_tts_time = time.time()  # Update time
                                recent_frames = recent_frames[-MIN_CONSECUTIVE_SPEECH:]
                            else:
                                # Tidak ada TTS yang jalan, tapi ada speech - siap untuk capture
                                speech_active = True
                                # Calculate average energy for last few frames
                                try:
                                    avg_energy = sum(audioop.rms(f, 2) for f in recent_frames[-5:]) / min(5, len(recent_frames))
                                except:
                                    avg_energy = rms
                                logger.info(f"🎤 Human speech detected (consecutive: {consecutive_speech_frames}, current energy: {rms}, avg: {avg_energy:.0f})")

                    else:
                        # Reset consecutive counter jika ada silence
                        consecutive_speech_frames = 0
                        
                        if speech_active:
                            silence_counter += 1
                            if silence_counter > END_SILENCE_FRAMES:
                                speech_active = False
                                silence_counter = 0

                                if len(recent_frames) < MIN_UTTERANCE_FRAMES:
                                    logger.info(f"Too short utterance ({len(recent_frames)} frames), skipping.")
                                    recent_frames.clear()
                                    continue

                                # Selesai bicara, kirim ke STT
                                logger.info(f"End of utterance detected ({len(recent_frames)} frames), sending to STT.")
                                ulaw_frames = [audioop.lin2ulaw(b''.join(recent_frames), 2)]
                                websocket.last_tts_time = time.time()  # Mark untuk grace period
                                await process_twilio_audio(websocket, stream_sid, ulaw_frames)
                                recent_frames.clear()
                        else:
                            # Jika ada frames terkumpul tapi belum active, clear jika terlalu lama diam
                            if len(recent_frames) > 0 and silence_counter > END_SILENCE_FRAMES * 2:
                                recent_frames.clear()
                                consecutive_speech_frames = 0

            elif event == "mark":
                mark_name = data["mark"]["name"]
                if mark_name == "end_of_speech":
                    # Update timestamp saat TTS selesai
                    websocket.last_tts_time = time.time()
                logger.info(f"Mark received: {mark_name}")

            elif event == "stop":
                logger.info("Stream stop event")
                break

    except Exception as e:
        logger.exception(f"Unhandled exception in twilio websocket: {e}")
    finally:
        # cleanup any tasks for this stream
        await stop_tts_playback(stream_sid)
        logger.info(f"Cleaned up stream {stream_sid}")

async def transcribe_audio(audio_path: str) -> str:
    """
    Transkripsi audio menggunakan ElevenLabs STT
    Support WAV (Twilio) dan WebM (webcall)
    """
    stt_url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    data = {"model_id": "scribe_v1"}
    
    try:
        logger.info(f"Transcribing audio: {audio_path}")
        
        # Deteksi format berdasarkan extension
        is_wav = audio_path.lower().endswith('.wav')
        is_webm = audio_path.lower().endswith('.webm')
        
        # Untuk WAV (dari Twilio): langsung kirim tanpa konversi
        if is_wav:
            logger.info("Detected WAV format (Twilio), sending directly to STT...")
            async with aiofiles.open(audio_path, "rb") as audio_file:
                audio_bytes = await audio_file.read()
            
            # Kirim langsung sebagai WAV
            files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
            
        # Untuk WebM (dari webcall): convert ke WAV dengan optimal settings
        elif is_webm:
            logger.info("Detected WebM format (webcall), converting to WAV...")
            try:
                from pydub import AudioSegment
                from pydub.utils import which
                
                def convert_with_pydub(audio_path):
                    # Set ffmpeg path jika ada
                    AudioSegment.converter = which("ffmpeg") or "ffmpeg"
                    
                    # Load WebM
                    audio = AudioSegment.from_file(audio_path, format="webm")
                    
                    # Konversi ke format optimal untuk STT
                    audio = audio.set_channels(1)  # Mono
                    audio = audio.set_frame_rate(16000)  # 16kHz optimal untuk STT
                    
                    # Export ke WAV
                    wav_path = audio_path.replace('.webm', '.wav')
                    audio.export(wav_path, format="wav")
                    logger.info(f"Converted WebM to WAV: {wav_path} ({len(audio)}ms)")
                    
                    return wav_path
                
                wav_path = await asyncio.to_thread(convert_with_pydub, audio_path)
                
                # Kirim WAV file
                async with aiofiles.open(wav_path, "rb") as audio_file:
                    audio_bytes = await audio_file.read()
                
                files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
                
                # Cleanup converted WAV file
                try:
                    await asyncio.to_thread(os.remove, wav_path)
                except:
                    pass
                    
            except Exception as conv_error:
                logger.warning(f"WebM conversion failed: {conv_error}, trying direct...")
                
                # Fallback: Kirim langsung WebM
                async with aiofiles.open(audio_path, "rb") as audio_file:
                    audio_bytes = await audio_file.read()
                files = {"file": ("audio.webm", audio_bytes, "audio/webm")}
        
        # Unknown format: try as-is
        else:
            logger.warning(f"Unknown audio format: {audio_path}, trying direct...")
            async with aiofiles.open(audio_path, "rb") as audio_file:
                audio_bytes = await audio_file.read()
            
            # Guess mime type
            mime_type = "audio/wav" if ".wav" in audio_path else "audio/webm"
            files = {"file": (os.path.basename(audio_path), audio_bytes, mime_type)}
        
        # Kirim ke ElevenLabs STT
        async with httpx.AsyncClient() as client:
            response = await client.post(
                stt_url, 
                headers=headers, 
                data=data, 
                files=files, 
                timeout=60.0
            )
            
            if response.status_code != 200:
                logger.error(f"ElevenLabs STT API error: {response.status_code} - {response.text}")
                return ""
                
            response.raise_for_status()
            result = response.json().get("text", "").strip()
            logger.info(f"STT transcription result: '{result}'")
            
            # Filter out noise and meaningless audio
            if not result or len(result) < 3:
                logger.info("Audio too short or empty, skipping")
                return ""
            
            # Filter out ONLY pure noise (not actual speech with background noise)
            result_lower = result.lower().strip()
            
            # Check if it's ONLY noise markers without actual content
            pure_noise_patterns = [
                "(ambient noise)",
                "(background noise)", 
                "(noise)",
                "(silence)",
                "(breathing)",
                "(cough)",
                "(sigh)"
            ]
            
            # If the entire result is just a noise marker, skip it
            if result_lower in pure_noise_patterns:
                logger.info(f"Pure noise detected: '{result}', skipping")
                return ""
            
            # Remove noise markers from the beginning of transcription but keep the actual speech
            cleaned_result = result
            for pattern in pure_noise_patterns:
                # Remove noise markers at the start (case insensitive)
                import re
                cleaned_result = re.sub(r'^\(' + re.escape(pattern[1:-1]) + r'\)\s*', '', cleaned_result, flags=re.IGNORECASE)
            
            # Only skip if after removing noise markers, we have very short/meaningless content
            if len(cleaned_result.strip()) < 5:
                logger.info(f"Content too short after noise removal: '{cleaned_result}', skipping")
                return ""
            
            # Filter out very short interjections (but allow longer phrases)
            if len(cleaned_result.strip()) < 10:
                short_interjections = ["hmm", "uh", "um", "ah", "eh", "mm", "hm"]
                if cleaned_result.lower().strip() in short_interjections:
                    logger.info(f"Short interjection detected: '{cleaned_result}', skipping")
                    return ""
            
            logger.info(f"Final transcription after cleaning: '{cleaned_result}'")
            return cleaned_result.strip()
                
    except Exception as e:
        logger.error(f"STT Error: {e}", exc_info=True)
        return ""


# --- ENDPOINTS EXISTING (Chat WebSocket, Upload PDF, dll) ---

@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    try:
        if not file.filename.lower().endswith('.pdf'):
            raise HTTPException(status_code=400, detail="File harus berupa PDF")
        
        pdf_content = await file.read()
        if len(pdf_content) == 0:
            raise HTTPException(status_code=400, detail="File PDF kosong")
        
        logger.info(f"Memproses PDF: {file.filename}")
        documents = await process_pdf_to_documents(pdf_content, file.filename)
        
        if not documents:
            raise HTTPException(status_code=400, detail="Tidak ada teks yang dapat diekstrak dari PDF")
        
        num_chunks = await add_documents_to_pinecone(documents)
        
        global index
        try:
            def _update_index():
                pc = Pinecone(api_key=PINECONE_API_KEY)
                pinecone_index = pc.Index(PINECONE_INDEX_NAME)
                vector_store = PineconeVectorStore(pinecone_index=pinecone_index)
                return VectorStoreIndex.from_vector_store(vector_store=vector_store)
            
            index = await asyncio.to_thread(_update_index)
            logger.info("Index berhasil diperbarui dengan dokumen baru")
        except Exception as e:
            logger.warning(f"Gagal memperbarui index: {e}")
        
        return JSONResponse(content={
            "message": f"PDF berhasil diupload dan diindex",
            "filename": file.filename,
            "pages_processed": len(documents),
            "chunks_created": num_chunks
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading PDF: {e}")
        raise HTTPException(status_code=500, detail=f"Error uploading PDF: {str(e)}")

@app.get("/index-stats")
async def get_index_stats():
    try:
        def _get_stats():
            pc = Pinecone(api_key=PINECONE_API_KEY)
            
            if PINECONE_INDEX_NAME not in pc.list_indexes().names():
                return {
                    "total_vectors": 0,
                    "dimension": 768,
                    "index_fullness": 0,
                    "status": "Index belum dibuat",
                    "documents": [],
                    "index_name": PINECONE_INDEX_NAME
                }
            
            pinecone_index = pc.Index(PINECONE_INDEX_NAME)
            stats = pinecone_index.describe_index_stats()
            
            documents = set()
            try:
                query_response = pinecone_index.query(
                    vector=[0.0] * 768,
                    top_k=1000,
                    include_metadata=True
                )
                for match in query_response.matches:
                    if match.metadata and 'file_name' in match.metadata:
                        documents.add(match.metadata['file_name'])
            except Exception as e:
                logger.warning(f"Tidak bisa mengambil daftar dokumen: {e}")
            
            return {
                "total_vectors": stats.get('total_vector_count', 0),
                "dimension": stats.get('dimension', 0),
                "index_fullness": stats.get('index_fullness', 0),
                "status": "Index aktif",
                "documents": list(documents),
                "index_name": PINECONE_INDEX_NAME
            }
        
        result = await asyncio.to_thread(_get_stats)
        return JSONResponse(content=result)
        
    except Exception as e:
        logger.error(f"Error getting index stats: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting index stats: {str(e)}")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint untuk chat web (existing functionality)"""
    await websocket.accept()
    logger.info("Client connected.")
    try:
        while True:
            audio_chunks = []
            while True:
                message = await websocket.receive()
                if "bytes" in message:
                    audio_chunks.append(message["bytes"])
                elif "text" in message and message["text"] == "END_OF_STREAM":
                    logger.info("Menerima sinyal akhir stream, memproses...")
                    break
            
            if not audio_chunks:
                continue
            
            full_audio_bytes = b"".join(audio_chunks)
            temp_audio_path = "temp_audio_received.webm"
            
            async with aiofiles.open(temp_audio_path, "wb") as f:
                await f.write(full_audio_bytes)
            
            logger.info("Memulai STT...")
            transcribed_text = ""
            stt_url = "https://api.elevenlabs.io/v1/speech-to-text"
            headers_stt = {"xi-api-key": ELEVENLABS_API_KEY}
            data_stt = {"model_id": "scribe_v1"}
            
            try:
                async with aiofiles.open(temp_audio_path, "rb") as audio_file:
                    audio_bytes = await audio_file.read()
                
                files = {"file": (os.path.basename(temp_audio_path), audio_bytes, "audio/webm")}
                async with httpx.AsyncClient() as client:
                    response = await client.post(stt_url, headers=headers_stt, data=data_stt, files=files, timeout=60.0)
                    response.raise_for_status()
                    transcribed_text = response.json().get("text", "").strip()
                logger.info(f"Transkripsi: \"{transcribed_text}\"")
            finally:
                await asyncio.to_thread(os.remove, temp_audio_path)
            
            await websocket.send_json({"type": "user_transcript", "text": transcribed_text})
            
            if transcribed_text and index is not None:
                start_time = time.time()
                logger.info("Memulai pipeline optimized RAG -> TTS...")
                
                # Query RAG dengan cache dan timeout (sama seperti Twilio)
                cache_key = transcribed_text.lower().strip()
                now = time.time()
                
                response_text = None
                if cache_key in rag_cache:
                    cached_entry = rag_cache[cache_key]
                    if now - cached_entry['timestamp'] < rag_cache_ttl:
                        response_text = cached_entry['response']
                        logger.info(f"✨ Cache hit! Skipped RAG query")
                
                # If not in cache, query RAG with standard method
                if response_text is None:
                    try:
                        rag_start = time.time()
                        query_engine = index.as_query_engine(
                            streaming=False,
                            text_qa_template=qa_prompt_tmpl,
                            similarity_top_k=3
                        )
                        
                        # Set timeout 5 detik
                        response = await asyncio.wait_for(
                            query_engine.aquery(transcribed_text),
                            timeout=5.0
                        )
                        response_text = response.response.strip()
                        
                        # Save to cache
                        rag_cache[cache_key] = {
                            'response': response_text,
                            'timestamp': now
                        }
                        rag_time = time.time() - rag_start
                        logger.info(f"⚡ RAG query completed in {rag_time:.2f}s")
                        
                    except asyncio.TimeoutError:
                        logger.error("⏱️ Webcall RAG query timeout after 5s!")
                        response_text = "Maaf, sistem sedang lambat. Bisa ulangi pertanyaan Anda?"
                    except Exception as e:
                        logger.exception(f"❌ Webcall RAG error: {e}")
                        response_text = "Maaf, terjadi kesalahan saat mencari informasi."
                
                # Send response text
                await websocket.send_json({"type": "bot_response", "text": response_text})
                
                # Stream TTS dengan optimasi low-latency
                voice_id = "JBFqnCBsd6RMkjVDRZzb"
                tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
                headers_tts = {
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "text": response_text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": 0.65,
                        "similarity_boost": 0.75,
                        "style": 0.0,
                        "use_speaker_boost": True
                    },
                    "optimize_streaming_latency": 4  # Max optimization
                }
                
                tts_start = time.time()
                first_chunk_received = False
                
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", tts_url, headers=headers_tts, json=payload) as tts_response:
                        tts_response.raise_for_status()
                        async for audio_chunk in tts_response.aiter_bytes():
                            if not first_chunk_received:
                                first_audio_time = time.time() - tts_start
                                logger.info(f"🎵 First TTS audio in {first_audio_time:.3f}s")
                                first_chunk_received = True
                            await websocket.send_bytes(audio_chunk)
                
                total_time = time.time() - start_time
                logger.info(f"⚡ Webcall total pipeline: {total_time:.2f}s")
            
            logger.info("Satu giliran percakapan selesai. Menunggu input berikutnya.")
    
    except WebSocketDisconnect:
        logger.info("Client terputus.")
    except Exception as e:
        logger.error(f"An error occurred: {e}", exc_info=True)

@app.websocket("/voice-call")
async def voice_call_websocket(websocket: WebSocket):
    """
    WebSocket endpoint untuk voice call tanpa Twilio
    Menangani audio real-time untuk percakapan langsung
    """
    await websocket.accept()
    logger.info("Voice call client connected.")
    
    audio_buffer = []
    call_session_id = f"call_{int(time.time())}"
    
    try:
        while True:
            message = await websocket.receive()
            
            if "bytes" in message:
                # Terima audio blob dari client (merged chunks)
                audio_blob = message["bytes"]
                audio_buffer.append(audio_blob)
                logger.info(f"Received audio blob: {len(audio_blob)} bytes (total blobs: {len(audio_buffer)})")
                
            elif "text" in message:
                data = json.loads(message["text"])
                
                if data.get("type") == "START_CALL":
                    logger.info("Voice call started")
                    await websocket.send_json({
                        "type": "call_status", 
                        "status": "connected",
                        "message": "Voice call berhasil terhubung. Mulai berbicara!"
                    })
                    
                    # Kirim greeting
                    await send_voice_call_greeting(websocket)
                
                elif data.get("type") == "END_AUDIO":
                    # Proses audio yang sudah terkumpul
                    if audio_buffer:
                        await process_voice_call_audio_buffer(websocket, audio_buffer, call_session_id)
                        audio_buffer = []  # Clear buffer after processing
                
                elif data.get("type") == "END_CALL":
                    logger.info("Voice call ended")
                    break
    
    except WebSocketDisconnect:
        logger.info("Voice call client disconnected.")
    except Exception as e:
        logger.error(f"Error in voice call WebSocket: {e}", exc_info=True)
    finally:
        # Cleanup temp files
        for file in glob.glob(f"temp_voice_call_{call_session_id}*.webm"):
            try:
                await asyncio.to_thread(os.remove, file)
            except:
                pass


async def send_voice_call_greeting(websocket: WebSocket):
    """Kirim greeting audio untuk voice call"""
    try:
        greeting_text = "Halo! Saya adalah asisten AI Anda. Apa yang bisa saya bantu hari ini?"
        
        voice_id = "JBFqnCBsd6RMkjVDRZzb"
        tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "text": greeting_text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.7,  # Lebih stabil
                "similarity_boost": 0.8,  # Lebih natural
                "style": 0.2,  # Sedikit style untuk naturalness
                "use_speaker_boost": True  # Boost speaker quality
            }
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(tts_url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            audio_content = response.content
            
            # Kirim audio sebagai bytes
            await websocket.send_bytes(audio_content)
            
        logger.info("Greeting audio sent to voice call client")
        
    except Exception as e:
        logger.error(f"Error sending greeting: {e}")

async def process_voice_call_audio_buffer(websocket: WebSocket, audio_buffer: list, session_id: str):
    """Proses audio buffer dari voice call: STT -> RAG -> TTS"""
    try:
        logger.info(f"Processing voice call audio buffer with {len(audio_buffer)} chunks...")
        
        # Gabungkan audio chunks
        full_audio = b''.join(audio_buffer)
        logger.info(f"Total audio size: {len(full_audio)} bytes")
        
        if len(full_audio) == 0:
            logger.warning("Empty audio buffer received")
            return
        
        # Simpan sebagai file sementara
        temp_audio_path = f"temp_voice_call_{session_id}.webm"
        async with aiofiles.open(temp_audio_path, "wb") as f:
            await f.write(full_audio)
        
        logger.info(f"Audio saved to: {temp_audio_path}")
        
        # STT dengan ElevenLabs
        transcribed_text = await transcribe_audio(temp_audio_path)
        logger.info(f"Voice call transcription: '{transcribed_text}'")
        
        # Cleanup temp file dengan retry
        try:
            await asyncio.sleep(0.1)  # Wait a bit for file to be released
            await asyncio.to_thread(os.remove, temp_audio_path)
            logger.info(f"Cleaned up temp file: {temp_audio_path}")
        except Exception as cleanup_error:
            logger.warning(f"Could not delete temp file {temp_audio_path}: {cleanup_error}")
        
        if not transcribed_text or len(transcribed_text.strip()) < 3:
            logger.warning("Transcription too short or empty")
            await send_voice_call_response(
                websocket,
                "Maaf, saya tidak mendengar dengan jelas. Bisa ulangi lagi?"
            )
            return
        
        # Kirim transcript ke client
        await websocket.send_json({
            "type": "user_transcript",
            "text": transcribed_text
        })
        
        # Query RAG
        if index is not None:
            logger.info("Querying RAG for voice call...")
            query_engine = index.as_query_engine(
                streaming=False,
                text_qa_template=qa_prompt_tmpl,
                similarity_top_k=10
            )
            response = await query_engine.aquery(transcribed_text)
            response_text = response.response.strip()
            logger.info(f"Voice call RAG response: {response_text}")
        else:
            response_text = "Maaf, sistem sedang tidak tersedia. Silakan coba lagi nanti."
        
        # Kirim response text ke client
        await websocket.send_json({
            "type": "ai_response",
            "text": response_text
        })
        
        # TTS dan kirim response
        await send_voice_call_response(websocket, response_text)
        
    except Exception as e:
        logger.error(f"Error processing voice call audio buffer: {e}", exc_info=True)
        await send_voice_call_response(
            websocket,
            "Maaf, terjadi kesalahan. Silakan coba lagi."
        )

async def send_voice_call_response(websocket: WebSocket, text: str):
    """Generate TTS dan kirim ke voice call client"""
    try:
        voice_id = "JBFqnCBsd6RMkjVDRZzb"
        tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.7,  # Lebih stabil
                "similarity_boost": 0.8,  # Lebih natural
                "style": 0.2,  # Sedikit style untuk naturalness
                "use_speaker_boost": True  # Boost speaker quality
            }
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(tts_url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            audio_content = response.content
            
            # Kirim audio sebagai bytes
            await websocket.send_bytes(audio_content)
            
        logger.info(f"Voice call response sent: {text[:50]}...")
        
    except Exception as e:
        logger.error(f"Error sending voice call response: {e}", exc_info=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)