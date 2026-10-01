// Voice Call RAG Assistant - Real-time conversation (FIXED VERSION)
class VoiceCallRAG {
    constructor() {
        this.socket = null;
        this.mediaRecorder = null;
        this.audioContext = null;
        this.analyser = null;
        this.isCallActive = false;
        this.isMuted = false;
        this.audioQueue = [];
        this.isPlaying = false;
        this.currentAudio = null;
        this.volumeCheckInterval = null;
        this.silenceCheckInterval = null;
        this.audioChunks = [];

        // Speech detection variables
        this.isSpeaking = false;
        this.silenceStartTime = null;
        this.SILENCE_THRESHOLD = 1000; // 1 second (reduced for faster response)
        this.VOLUME_THRESHOLD = 0.1; // 8% volume (slightly more sensitive)

        // Interruption settings
        this.interruptionSettings = {
            volumeThreshold: 0.3,
            delayMs: 500,
            enabled: true
        };

        // Twilio phone call
        this.twilioDevice = null;
        this.isPhoneCallActive = false;

        // DOM elements
        this.statusIndicator = document.getElementById('status-indicator');
        this.statusText = document.getElementById('status-text');
        this.volumeBar = document.getElementById('volume-bar');
        this.conversationLog = document.getElementById('conversation-log');
        this.startCallBtn = document.getElementById('start-call-btn');
        this.muteBtn = document.getElementById('mute-btn');
        this.endCallBtn = document.getElementById('end-call-btn');
        this.interruptionIndicator = document.getElementById('interruption-indicator');

        this.init();
    }

    init() {
        this.startCallBtn.addEventListener('click', () => this.startCall());
        this.muteBtn.addEventListener('click', () => this.toggleMute());
        this.endCallBtn.addEventListener('click', () => this.endCall());

        // Auto-connect on page load
        this.connectWebSocket();
    }

    connectWebSocket() {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) return;

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        const ws_url = `${protocol}//${host}/voice-call`;

        console.log("Connecting to WebSocket:", ws_url);
        this.updateStatus('connecting', 'Menghubungkan...');

        this.socket = new WebSocket(ws_url);

        this.socket.onopen = () => {
            console.log("✅ WebSocket connected successfully");
            this.updateStatus('connected', 'Siap untuk panggilan');
        };

        this.socket.onclose = (event) => {
            console.log("❌ WebSocket disconnected:", event.code, event.reason);
            this.updateStatus('disconnected', 'Koneksi terputus');
            this.isCallActive = false;
            this.updateUI();

            // Auto-reconnect after 3 seconds
            setTimeout(() => {
                console.log("🔄 Attempting to reconnect...");
                this.connectWebSocket();
            }, 3000);
        };

        this.socket.onerror = (error) => {
            console.error("❌ WebSocket error:", error);
            this.updateStatus('disconnected', 'Error koneksi');
        };

        this.socket.onmessage = async (event) => {
            if (event.data instanceof Blob) {
                console.log("🔊 Received audio blob:", event.data.size, "bytes");
                this.playAudioStream(event.data);
            } else {
                const message = JSON.parse(event.data);
                console.log("📨 Received message:", message.type);

                if (message.type === 'call_status') {
                    console.log("📞 Call status:", message.status, message.message);
                    this.updateStatus('connected', message.message || 'Call connected');
                } else if (message.type === 'user_transcript') {
                    this.addMessage(message.text, 'user');
                } else if (message.type === 'ai_response') {
                    this.addMessage(message.text, 'bot');
                } else if (message.type === 'tts_complete') {
                    console.log("✅ TTS completed, ready for next input");
                    this.updateStatus('connected', 'Mendengarkan...');
                } else if (message.type === 'error') {
                    console.error("❌ Server error:", message.message);
                    this.addMessage(message.message, 'system');
                }
            }
        };
    }

    updateStatus(status, text) {
        this.statusIndicator.className = `status-indicator status-${status}`;
        this.statusText.textContent = text;
    }

    async startCall() {
        if (this.isCallActive) return;

        try {
            console.log("🎤 Starting call...");

            // Get microphone permission
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    sampleRate: 48000
                }
            });

            this.isCallActive = true;
            this.updateStatus('connected', 'Panggilan aktif - Berbicara sekarang');
            this.updateUI();

            // Setup audio analysis
            this.setupAudioAnalysis(stream);

            // Start continuous recording
            this.startContinuousRecording(stream);

            // Send START_CALL signal to server
            if (this.socket && this.socket.readyState === WebSocket.OPEN) {
                this.socket.send(JSON.stringify({ type: 'START_CALL' }));
                console.log("📞 Sent START_CALL signal to server");
            }

            this.addMessage("✅ Panggilan dimulai. Silakan mulai berbicara.", 'system');

        } catch (error) {
            console.error("❌ Error starting call:", error);
            this.addMessage("Error: Tidak dapat mengakses mikrofon. Pastikan izin mikrofon diberikan.", 'system');
        }
    }

    setupAudioAnalysis(stream) {
        this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = this.audioContext.createMediaStreamSource(stream);
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.smoothingTimeConstant = 0.8;
        source.connect(this.analyser);

        console.log("🎵 Audio analysis setup complete");

        // Start volume monitoring
        this.startVolumeMonitoring();
        this.startSilenceDetection();
    }

    startVolumeMonitoring() {
        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        let userSpeakingStart = 0;

        const updateVolume = () => {
            if (!this.isCallActive || !this.analyser) return;

            this.analyser.getByteFrequencyData(dataArray);
            const average = dataArray.reduce((a, b) => a + b) / dataArray.length;
            const volume = Math.min(100, (average / 255) * 100);

            this.volumeBar.style.width = `${volume}%`;

            // Check for user interruption (only if enabled and AI is speaking)
            if (this.interruptionSettings.enabled && this.isPlaying) {
                const currentTime = Date.now();
                const normalizedVolume = volume / 100;

                if (normalizedVolume > this.interruptionSettings.volumeThreshold) {
                    if (userSpeakingStart === 0) {
                        userSpeakingStart = currentTime;
                    } else if (currentTime - userSpeakingStart > this.interruptionSettings.delayMs) {
                        this.interruptAI();
                        userSpeakingStart = 0;
                    }
                } else {
                    userSpeakingStart = 0;
                }
            }

            if (this.isCallActive) {
                requestAnimationFrame(updateVolume);
            }
        };

        updateVolume();
    }

    startSilenceDetection() {
        console.log("🔇 Starting silence detection");
        console.log("- Silence threshold:", this.SILENCE_THRESHOLD + "ms");
        console.log("- Volume threshold:", this.VOLUME_THRESHOLD);

        this.silenceCheckInterval = setInterval(() => {
            if (!this.analyser || !this.isCallActive) return;

            const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
            this.analyser.getByteFrequencyData(dataArray);

            const average = dataArray.reduce((sum, value) => sum + value, 0) / dataArray.length;
            const volume = average / 255;

            // Log occasionally for debugging
            if (Math.random() < 0.02) {  // Reduced frequency
                console.log(`📊 Volume: ${volume.toFixed(4)}, Speaking: ${this.isSpeaking}, Recording: ${this.mediaRecorder?.state}, AI Playing: ${this.isPlaying}`);
            }

            // ALWAYS check for user speech, even when AI is playing (for interruption)
            if (volume > this.VOLUME_THRESHOLD) {
                // Speech detected
                if (!this.isSpeaking) {
                    this.isSpeaking = true;
                    this.silenceStartTime = null;
                    console.log(`🗣️ Speech started - isPlaying: ${this.isPlaying}, interruption enabled: ${this.interruptionSettings.enabled}`);

                    // INTERRUPTION: If user speaks while AI is speaking, interrupt AI
                    if (this.isPlaying && this.interruptionSettings.enabled) {
                        console.log("🛑 User speaking detected - interrupting AI");
                        this.interruptAI();
                    } else if (!this.isPlaying) {
                        this.updateStatus('connected', 'Berbicara...');
                        console.log("ℹ️ User speaking (AI not playing)");
                    }
                }
            } else {
                // Silence detected
                if (this.isSpeaking) {
                    this.isSpeaking = false;
                    this.silenceStartTime = Date.now();
                    console.log("🤫 Speech ended, starting silence timer");
                    if (!this.isPlaying) {
                        this.updateStatus('connected', 'Mendengarkan...');
                    }
                }

                // Only process silence (send audio) when AI is NOT playing
                if (!this.isPlaying) {
                    // Check if silence has been long enough
                    if (this.silenceStartTime && Date.now() - this.silenceStartTime > this.SILENCE_THRESHOLD) {
                        // Stop recorder to send audio
                        if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
                            console.log(`📤 Silence threshold reached (${this.SILENCE_THRESHOLD}ms), stopping recorder...`);
                            this.stopAndRestartRecorder();
                        }
                        this.silenceStartTime = null;
                    }
                }
            }
        }, 100);
    }

    stopAndRestartRecorder() {
        if (!this.mediaRecorder || this.mediaRecorder.state !== 'recording') return;

        // Stop akan trigger ondataavailable + onstop yang akan send audio
        this.mediaRecorder.stop();

        // Restart after a short delay with same stream
        setTimeout(() => {
            if (this.isCallActive && !this.isMuted && this.currentStream) {
                this.startContinuousRecording(this.currentStream);
            }
        }, 100);
    }

    startContinuousRecording(stream) {
        // Save stream reference
        this.currentStream = stream;

        // Try different audio formats
        let mimeType = 'audio/webm;codecs=opus';
        if (!MediaRecorder.isTypeSupported(mimeType)) {
            mimeType = 'audio/webm';
            if (!MediaRecorder.isTypeSupported(mimeType)) {
                mimeType = 'audio/mp4';
            }
        }

        this.mediaRecorder = new MediaRecorder(stream, {
            mimeType: mimeType,
            audioBitsPerSecond: 128000
        });

        console.log(`🎙️ Using audio format: ${mimeType}`);

        this.audioChunks = [];

        this.mediaRecorder.ondataavailable = (event) => {
            // When start() without timeslice, this is called ONCE on stop() with complete data
            if (event.data.size > 0) {
                console.log(`📥 Complete audio data received: ${event.data.size} bytes`);

                // Don't send if muted or AI is speaking
                if (!this.isMuted && !this.isPlaying) {
                    // Send directly - this is already a complete WebM blob
                    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
                        console.log(`📤 Sending complete audio blob: ${event.data.size} bytes`);
                        this.socket.send(event.data);
                        this.socket.send(JSON.stringify({ type: 'END_AUDIO' }));
                        console.log(`✅ Sent complete audio`);
                        this.updateStatus('connected', 'Memproses...');
                    }
                } else {
                    console.log(`⏭️ Skipping audio - muted: ${this.isMuted}, AI playing: ${this.isPlaying}`);
                }
            } else {
                console.log(`⚠️ No audio data captured (size: 0)`);
            }
        };

        this.mediaRecorder.onstop = () => {
            console.log(`⏹️ MediaRecorder stopped`);
            // ondataavailable already handled sending
            // Just clear the array
            this.audioChunks = [];
        };

        this.mediaRecorder.onerror = (event) => {
            console.error("❌ MediaRecorder error:", event.error);
        };

        // Start recording WITHOUT timeslice to get complete file on stop
        // This is KEY difference from previous approach
        this.mediaRecorder.start(); // No timeslice = complete file on stop
        console.log("✅ MediaRecorder started - will generate complete file on stop");
    }

    // sendAudioChunks is now handled in onstop event

    toggleMute() {
        this.isMuted = !this.isMuted;
        this.muteBtn.innerHTML = this.isMuted ?
            '<i class="bi bi-mic-mute-fill"></i>' :
            '<i class="bi bi-mic-fill"></i>';
        this.muteBtn.style.background = this.isMuted ? '#dc3545' : '#ffc107';

        console.log(this.isMuted ? "🔇 Muted" : "🔊 Unmuted");
        this.addMessage(this.isMuted ? "🔇 Mikrofon dimute" : "🔊 Mikrofon aktif", 'system');
    }

    endCall() {
        console.log("📞 Ending call...");

        this.isCallActive = false;

        // CRITICAL: Stop any playing audio immediately
        if (this.currentAudio) {
            try {
                this.currentAudio.pause();
                this.currentAudio.currentTime = 0;
                this.currentAudio.src = ''; // Clear source to fully stop
                this.currentAudio = null;
                console.log("🔇 Stopped AI audio playback");
            } catch (e) {
                console.error("Error stopping audio:", e);
            }
        }

        // Clear audio queue
        this.audioQueue = [];
        this.isPlaying = false;

        // Stop recording
        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
        }

        // Stop silence detection
        if (this.silenceCheckInterval) {
            clearInterval(this.silenceCheckInterval);
            this.silenceCheckInterval = null;
        }

        // Stop volume monitoring
        if (this.volumeCheckInterval) {
            clearInterval(this.volumeCheckInterval);
            this.volumeCheckInterval = null;
        }

        // Stop audio stream
        if (this.currentStream) {
            this.currentStream.getTracks().forEach(track => track.stop());
            this.currentStream = null;
        }

        // Close audio context
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }

        // Send END_CALL signal to server
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify({ type: 'END_CALL' }));
            console.log("📞 Sent END_CALL signal to server");
        }

        // Clear any remaining audio
        this.audioChunks = [];

        // Update UI
        this.updateStatus('connected', 'Panggilan diakhiri');
        this.updateUI();

        this.addMessage("📞 Panggilan diakhiri.", 'system');
    }

    updateUI() {
        if (this.isCallActive) {
            this.startCallBtn.style.display = 'none';
            this.muteBtn.style.display = 'block';
            this.endCallBtn.style.display = 'block';
        } else {
            this.startCallBtn.style.display = 'block';
            this.muteBtn.style.display = 'none';
            this.endCallBtn.style.display = 'none';
        }
    }

    addMessage(text, sender) {
        if (!text) return;

        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message');

        if (sender === 'user') {
            messageDiv.classList.add('user-message');
            messageDiv.innerHTML = `<strong>Anda:</strong> ${text}`;
        } else if (sender === 'bot') {
            messageDiv.classList.add('bot-message');
            messageDiv.innerHTML = `<strong>AI:</strong> ${text}`;
        } else {
            messageDiv.style.background = '#e9ecef';
            messageDiv.style.textAlign = 'center';
            messageDiv.innerHTML = `<small><em>${text}</em></small>`;
        }

        this.conversationLog.appendChild(messageDiv);
        this.conversationLog.scrollTop = this.conversationLog.scrollHeight;
    }

    playAudioStream(audioBlob) {
        console.log("🔊 Adding audio to queue:", audioBlob.size, "bytes");
        this.audioQueue.push(audioBlob);
        if (!this.isPlaying) {
            this.playNextInQueue();
        }
    }

    playNextInQueue() {
        if (this.audioQueue.length === 0) {
            this.isPlaying = false;
            this.currentAudio = null;
            console.log("✅ Audio queue empty, ready for next input");
            this.updateStatus('connected', 'Mendengarkan...');

            // CRITICAL: Clear audio chunks collected during AI speech
            console.log("🧹 Clearing audio chunks collected during AI speech");
            this.audioChunks = [];

            // Reset speaking state
            this.isSpeaking = false;
            this.silenceStartTime = null;
            return;
        }

        this.isPlaying = true;
        const blob = this.audioQueue.shift();
        const audioUrl = URL.createObjectURL(blob);
        this.currentAudio = new Audio(audioUrl);

        console.log("▶️ Playing audio from queue, remaining:", this.audioQueue.length);
        console.log("🔊 Current audio object created:", !!this.currentAudio);

        this.updateStatus('connected', 'AI berbicara... (bicara untuk interrupt)');

        // Setup event handlers BEFORE playing
        this.currentAudio.onended = () => {
            console.log("✅ Audio playback ended");
            URL.revokeObjectURL(audioUrl);
            this.playNextInQueue();
        };

        this.currentAudio.onerror = (error) => {
            console.error("❌ Error playing audio:", error);
            URL.revokeObjectURL(audioUrl);
            this.isPlaying = false;
            this.currentAudio = null;
            this.updateStatus('connected', 'Mendengarkan...');

            // Clear audio chunks on error too
            this.audioChunks = [];
        };

        // Play audio
        this.currentAudio.play().catch(err => {
            console.error("❌ Play failed:", err);
            this.isPlaying = false;
            this.currentAudio = null;
        });
    }

    interruptAI() {
        console.log(`🛑 interruptAI() called - isPlaying: ${this.isPlaying}, currentAudio exists: ${!!this.currentAudio}`);


        if (this.isPlaying && this.currentAudio) {
            console.log("🛑 User interrupted AI speech - STOPPING AUDIO NOW");

            // CRITICAL: Stop audio playback completely
            try {
                console.log("⏸️ Pausing audio...");
                this.currentAudio.pause();
                this.currentAudio.currentTime = 0;
                console.log("🗑️ Clearing audio source...");
                this.currentAudio.src = ''; // Clear source to fully stop
                this.currentAudio = null;
                console.log("✅ Audio stopped successfully");
            } catch (e) {
                console.error("❌ Error stopping audio:", e);
            }

            // Clear audio queue
            console.log(`🧹 Clearing audio queue (${this.audioQueue.length} items)`);
            this.audioQueue = [];
            this.isPlaying = false;

            // Show interruption indicator
            if (this.interruptionIndicator) {
                this.interruptionIndicator.style.display = 'block';
                setTimeout(() => {
                    this.interruptionIndicator.style.display = 'none';
                }, 2000);
            }

            this.addMessage("🛑 AI speech interrupted - listening to you...", 'system');

            // Clear audio chunks collected during AI speech
            this.audioChunks = [];

            // Reset silence detection
            this.silenceStartTime = null;
            this.isSpeaking = true; // User is speaking (that's why interrupt happened)

            // Restart recorder if needed to capture user's speech
            if (this.mediaRecorder && this.mediaRecorder.state === 'inactive' && this.currentStream) {
                console.log("🎙️ Restarting recorder after interruption");
                this.startContinuousRecording(this.currentStream);
            }

            this.updateStatus('connected', 'Mendengarkan Anda...');
        }
    }

    async startPhoneCall() {
        try {
            this.updateStatus('connecting', 'Memulai panggilan telepon...');
            this.addMessage("📞 Memulai panggilan telepon via Twilio...", 'system');

            if (!this.twilioCredentials) {
                await this.initializeTwilioDevice();
            }

            this.addMessage("✅ Phone call feature is ready!", 'system');
            this.addMessage("To make a real phone call:", 'system');
            this.addMessage("1. Call your Twilio number: " + this.twilioCredentials.phone_number, 'system');
            this.addMessage("2. AI will answer and help you", 'system');
            this.addMessage("3. Ask questions about your uploaded documents", 'system');

            this.isPhoneCallActive = true;
            this.updateStatus('active', 'Phone call ready');

        } catch (error) {
            console.error('❌ Error starting phone call:', error);
            this.updateStatus('error', 'Gagal memulai panggilan');
            this.addMessage(`❌ Error: ${error.message}`, 'system');
        }
    }

    async initializeTwilioDevice() {
        try {
            const response = await fetch('/twilio/token');
            const data = await response.json();

            if (!data.account_sid) {
                throw new Error('Failed to get Twilio credentials');
            }

            this.twilioCredentials = data;
            console.log('✅ Twilio credentials loaded successfully');
            this.addMessage("✅ Twilio credentials loaded. Ready for phone calls.", 'system');

        } catch (error) {
            console.error('❌ Error initializing Twilio Device:', error);
            this.addMessage(`❌ Twilio Error: ${error.message}`, 'system');
            throw error;
        }
    }
}

// Initialize when page loads
document.addEventListener('DOMContentLoaded', () => {
    console.log("🚀 Initializing Voice Call RAG Assistant...");
    new VoiceCallRAG();
});
