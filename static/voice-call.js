// Voice Call JavaScript
class VoiceCallManager {
    constructor() {
        this.socket = null;
        this.mediaRecorder = null;
        this.audioStream = null;
        this.pendingChunks = [];
        this.analyser = null;
        this.isPlaying = false;
        this.audioChunks = [];
        this.VOLUME_THRESHOLD = 0.1; // 0..1
        this.SILENCE_THRESHOLD_MS = 1500;
        this.isCallActive = false;
        this.isMuted = false;
        this.callStartTime = null;
        this.timerInterval = null;
        this.audioContext = null;
        this.currentAudio = null;
        
        // DOM Elements
        this.startCallBtn = document.getElementById('start-call-btn');
        this.endCallBtn = document.getElementById('end-call-btn');
        this.muteBtn = document.getElementById('mute-btn');
        this.callStatus = document.getElementById('call-status');
        this.callTimer = document.getElementById('call-timer');
        this.timerText = document.getElementById('timer-text');
        this.statusIndicator = document.getElementById('status-indicator');
        this.transcriptContainer = document.getElementById('transcript-container');
        this.transcriptContent = document.getElementById('transcript-content');
        
        this.initializeEventListeners();
    }
    
    initializeEventListeners() {
        this.startCallBtn.addEventListener('click', () => this.startCall());
        this.endCallBtn.addEventListener('click', () => this.endCall());
        this.muteBtn.addEventListener('click', () => this.toggleMute());
    }
    
    async startCall() {
        try {
            this.updateStatus('Menghubungkan...', 'connecting');
            this.startCallBtn.style.display = 'none';
            
            // Request microphone permission
            this.audioStream = await navigator.mediaDevices.getUserMedia({ 
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });
            
            // Connect to WebSocket
            await this.connectWebSocket();
            
            // Setup audio analysis and silence detection
            this.setupAudioAnalysis(this.audioStream);
            this.startSilenceDetection();
            
            // Start continuous recording
            this.startRecording();
            
            // Update UI
            this.isCallActive = true;
            this.callStartTime = Date.now();
            this.startTimer();
            this.updateStatus('Panggilan Aktif - Mulai berbicara', 'connected');
            this.endCallBtn.style.display = 'inline-block';
            this.muteBtn.style.display = 'inline-block';
            this.transcriptContainer.style.display = 'block';
            
            // Send start call signal
            this.socket.send(JSON.stringify({ type: 'START_CALL' }));
            
        } catch (error) {
            console.error('Error starting call:', error);
            this.updateStatus('Gagal memulai panggilan', 'disconnected');
            this.startCallBtn.style.display = 'inline-block';
            alert('Gagal mengakses mikrofon. Pastikan izin mikrofon diberikan.');
        }
    }
    
    async connectWebSocket() {
        return new Promise((resolve, reject) => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const host = window.location.host;
            const wsUrl = `${protocol}//${host}/voice-call`;
            
            console.log('Connecting to voice call WebSocket:', wsUrl);
            this.socket = new WebSocket(wsUrl);
            
            this.socket.onopen = () => {
                console.log('Voice call WebSocket connected');
                resolve();
            };
            
            this.socket.onclose = (event) => {
                console.log('Voice call WebSocket disconnected:', event.code, event.reason);
                this.handleDisconnection();
            };
            
            this.socket.onerror = (error) => {
                console.error('Voice call WebSocket error:', error);
                reject(error);
            };
            
            this.socket.onmessage = (event) => {
                this.handleWebSocketMessage(event);
            };
        });
    }
    
    handleWebSocketMessage(event) {
        if (event.data instanceof Blob) {
            // Audio data received
            this.playAudioResponse(event.data);
        } else {
            // Text message received
            const message = JSON.parse(event.data);
            this.handleTextMessage(message);
        }
    }
    
    handleTextMessage(message) {
        switch (message.type) {
            case 'call_status':
                if (message.status === 'connected') {
                    console.log('Call connected:', message.message);
                }
                break;
            case 'user_transcript':
                this.addTranscript(message.text, 'user');
                break;
            case 'ai_response':
                this.addTranscript(message.text, 'ai');
                break;
            default:
                console.log('Unknown message type:', message.type);
        }
    }
    
    startRecording() {
        if (!this.audioStream) return;
        
        // Choose supported mime type
        let mimeType = 'audio/webm;codecs=opus';
        if (!MediaRecorder.isTypeSupported(mimeType)) {
            mimeType = 'audio/webm';
            if (!MediaRecorder.isTypeSupported(mimeType)) {
                mimeType = 'audio/mp4';
            }
        }
        this.mediaRecorder = new MediaRecorder(this.audioStream, {
            mimeType,
            audioBitsPerSecond: 128000
        });
        
        this.mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0 && this.socket && this.socket.readyState === WebSocket.OPEN && !this.isMuted) {
                if (!this.isPlaying) {
                    this.audioChunks.push(event.data);
                    // console.log(`📥 Audio chunk received: ${event.data.size} bytes (total: ${this.audioChunks.length})`);
                } else {
                    // console.log(`⏭️ Skipping chunk during AI playback: ${event.data.size} bytes`);
                }
            }
        };
        
        this.mediaRecorder.onstop = () => {
            // no-op; sending is handled on silence or when ending call
        };
        
        // Start continuous recording (100ms like call.js)
        this.mediaRecorder.start(100);
        console.log('Recording started');
    }

    sendAudioChunks(chunks) {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN || this.isMuted) {
            console.log('⚠️ Cannot send audio - socket not ready or muted');
            return;
        }
        if (!chunks || chunks.length === 0) {
            console.log('⚠️ No audio chunks to send');
            return;
        }
        
        console.log(`📤 Sending ${chunks.length} audio chunks to server...`);
        let totalBytes = 0;
        chunks.forEach(chunk => {
            this.socket.send(chunk);
            totalBytes += chunk.size;
        });
        this.socket.send(JSON.stringify({ type: 'END_AUDIO' }));
        console.log(`✅ Sent ${chunks.length} chunks (${totalBytes} bytes total)`);
        this.updateStatus('Memproses...', 'connected');
    }
    
    setupAudioAnalysis(stream) {
        this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = this.audioContext.createMediaStreamSource(stream);
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.smoothingTimeConstant = 0.8;
        source.connect(this.analyser);
    }

    startSilenceDetection() {
        console.log('🔇 Starting silence detection');
        console.log(`- Volume threshold: ${this.VOLUME_THRESHOLD}`);
        console.log(`- Silence threshold: ${this.SILENCE_THRESHOLD_MS}ms`);
        
        const check = () => {
            if (!this.isCallActive || !this.analyser) return;
            if (this.isPlaying) {
                this._silenceStart = null;
                requestAnimationFrame(check);
                return;
            }
            const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
            this.analyser.getByteFrequencyData(dataArray);
            const average = dataArray.reduce((sum, v) => sum + v, 0) / dataArray.length;
            const volume = average / 255;
            
            // Debug log (uncomment untuk debugging)
            // if (Math.random() < 0.05) console.log(`📊 Volume: ${volume.toFixed(4)}, Chunks: ${this.audioChunks.length}`);
            
            if (volume > this.VOLUME_THRESHOLD) {
                // User sedang bicara
                if (this._silenceStart !== null) {
                    console.log('🗣️ Speech detected, resetting silence timer');
                }
                this._silenceStart = null;
            } else {
                // User diam
                if (this._silenceStart == null) {
                    this._silenceStart = Date.now();
                    console.log('🤫 Silence started');
                }
                if (Date.now() - this._silenceStart > this.SILENCE_THRESHOLD_MS) {
                    if (this.audioChunks.length > 0 && !this.isPlaying) {
                        console.log(`📤 Silence threshold reached, sending ${this.audioChunks.length} chunks`);
                        this.sendAudioChunks([...this.audioChunks]);
                        this.audioChunks = [];
                    }
                    this._silenceStart = null;
                }
            }
            requestAnimationFrame(check);
        };
        requestAnimationFrame(check);
    }
    
    
    stopRecording() {
        if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
            console.log('Stopping recording and sending audio for processing...');
            this.mediaRecorder.stop();
        }
    }
    
    playAudioResponse(audioBlob) {
        try {
            // Stop any currently playing audio
            if (this.currentAudio) {
                this.currentAudio.pause();
                this.currentAudio = null;
            }
            
            const audioUrl = URL.createObjectURL(audioBlob);
            this.currentAudio = new Audio(audioUrl);
            
            this.currentAudio.onloadeddata = () => {
                this.updateStatus('AI sedang berbicara...', 'connected');
                this.isPlaying = true;
            };
            
            this.currentAudio.onended = () => {
                URL.revokeObjectURL(audioUrl);
                this.currentAudio = null;
                this.updateStatus('Panggilan Aktif - Mulai berbicara', 'connected');
                this.isPlaying = false;
            };
            
            this.currentAudio.onerror = (error) => {
                console.error('Audio playback error:', error);
                URL.revokeObjectURL(audioUrl);
                this.currentAudio = null;
            };
            
            this.currentAudio.play().catch(error => {
                console.error('Error playing audio:', error);
            });
            
        } catch (error) {
            console.error('Error handling audio response:', error);
        }
    }
    
    toggleMute() {
        this.isMuted = !this.isMuted;
        
        if (this.audioStream) {
            this.audioStream.getAudioTracks().forEach(track => {
                track.enabled = !this.isMuted;
            });
        }
        
        // Stop or start recording based on mute state
        if (this.isMuted) {
            this.stopRecording();
        } else if (this.isCallActive) {
            this.startRecording();
        }
        
        this.muteBtn.innerHTML = this.isMuted ? 
            '<i class="bi bi-mic-mute-fill"></i>' : 
            '<i class="bi bi-mic-fill"></i>';
        
        this.muteBtn.style.background = this.isMuted ? 
            'linear-gradient(135deg, #dc3545, #c82333)' : 
            'linear-gradient(135deg, #ffc107, #e0a800)';
            
        console.log('Mute toggled:', this.isMuted);
    }
    
    endCall() {
        this.isCallActive = false;
        
        // Stop recording
        this.stopRecording();
        
        // Stop audio stream
        if (this.audioStream) {
            this.audioStream.getTracks().forEach(track => track.stop());
            this.audioStream = null;
        }
        
        // Stop current audio
        if (this.currentAudio) {
            this.currentAudio.pause();
            this.currentAudio = null;
        }
        
        // Close audio context
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        
        // Clear audio chunks
        this.audioChunks = [];
        this.isPlaying = false;
        
        // Close WebSocket
        if (this.socket) {
            this.socket.send(JSON.stringify({ type: 'END_CALL' }));
            this.socket.close();
            this.socket = null;
        }
        
        // Stop timer
        this.stopTimer();
        
        // Reset UI
        this.updateStatus('Panggilan Berakhir', 'disconnected');
        this.startCallBtn.style.display = 'inline-block';
        this.endCallBtn.style.display = 'none';
        this.muteBtn.style.display = 'none';
        this.callTimer.style.display = 'none';
        this.transcriptContainer.style.display = 'none';
        
        // Reset mute state
        this.isMuted = false;
        this.muteBtn.innerHTML = '<i class="bi bi-mic-fill"></i>';
        this.muteBtn.style.background = 'linear-gradient(135deg, #ffc107, #e0a800)';
        
        console.log('📞 Call ended and cleaned up');
    }
    
    handleDisconnection() {
        if (this.isCallActive) {
            this.updateStatus('Koneksi Terputus', 'disconnected');
            // Auto-reconnect after 3 seconds
            setTimeout(() => {
                if (this.isCallActive) {
                    this.connectWebSocket().catch(error => {
                        console.error('Reconnection failed:', error);
                        this.endCall();
                    });
                }
            }, 3000);
        }
    }
    
    updateStatus(message, status) {
        this.callStatus.textContent = message;
        
        // Update status indicator
        this.statusIndicator.className = `status-indicator status-${status}`;
        
        // Show/hide timer
        if (status === 'connected' && this.isCallActive) {
            this.callTimer.style.display = 'block';
        } else {
            this.callTimer.style.display = 'none';
        }
    }
    
    startTimer() {
        this.timerInterval = setInterval(() => {
            if (this.callStartTime) {
                const elapsed = Date.now() - this.callStartTime;
                const minutes = Math.floor(elapsed / 60000);
                const seconds = Math.floor((elapsed % 60000) / 1000);
                this.timerText.textContent = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
            }
        }, 1000);
    }
    
    stopTimer() {
        if (this.timerInterval) {
            clearInterval(this.timerInterval);
            this.timerInterval = null;
        }
    }
    
    addTranscript(text, sender) {
        if (!text || text.trim().length === 0) return;
        
        const transcriptItem = document.createElement('div');
        transcriptItem.className = `transcript-item ${sender === 'user' ? 'user-transcript' : 'ai-transcript'}`;
        transcriptItem.textContent = text;
        
        this.transcriptContent.appendChild(transcriptItem);
        this.transcriptContent.scrollTop = this.transcriptContent.scrollHeight;
    }
}

// Initialize voice call manager when page loads
document.addEventListener('DOMContentLoaded', () => {
    const voiceCallManager = new VoiceCallManager();
    
    // Handle page visibility change
    document.addEventListener('visibilitychange', () => {
        if (document.hidden && voiceCallManager.isCallActive) {
            console.log('Page hidden, call continues in background');
        }
    });
    
    // Handle beforeunload to properly end call
    window.addEventListener('beforeunload', (event) => {
        if (voiceCallManager.isCallActive) {
            voiceCallManager.endCall();
        }
    });
});
