// Twilio Voice Call Test - JavaScript (SDK v2.15)
class TwilioCallTest {
    constructor() {
        this.device = null;
        this.currentCall = null;
        this.isMuted = false;
        
        // DOM elements
        this.callBtn = document.getElementById('call-btn');
        this.hangupBtn = document.getElementById('hangup-btn');
        this.muteBtn = document.getElementById('mute-btn');
        this.statusText = document.getElementById('status-text');
        this.statusBadge = document.getElementById('status-badge');
        this.deviceStatus = document.getElementById('device-status');
        this.callStatus = document.getElementById('call-status');
        this.logsContainer = document.getElementById('logs-container');
        this.callAvatar = document.getElementById('call-avatar');
        
        this.init();
    }
    
    init() {
        this.log('Inisialisasi Twilio Client...', 'info');
        this.setupEventListeners();
        
        // Check if Twilio SDK is loaded
        if (typeof Twilio === 'undefined') {
            this.log('⏳ Menunggu Twilio SDK...', 'info');
            this.waitForTwilio();
        } else {
            this.initializeTwilioDevice();
        }
    }
    
    waitForTwilio() {
        let attempts = 0;
        const maxAttempts = 50; // 5 seconds max
        
        const checkTwilio = setInterval(() => {
            attempts++;
            
            if (typeof Twilio !== 'undefined') {
                clearInterval(checkTwilio);
                this.log('✅ Twilio SDK loaded!', 'success');
                this.initializeTwilioDevice();
            } else if (attempts >= maxAttempts) {
                clearInterval(checkTwilio);
                this.log('❌ Twilio SDK gagal dimuat. Pastikan script tag sudah benar.', 'error');
                this.updateStatus('Error: Twilio SDK not loaded', 'error');
                this.deviceStatus.textContent = 'Error - SDK not loaded';
            }
        }, 100);
    }
    
    setupEventListeners() {
        this.callBtn.addEventListener('click', () => this.makeCall());
        this.hangupBtn.addEventListener('click', () => this.hangupCall());
        this.muteBtn.addEventListener('click', () => this.toggleMute());
    }
    
    async initializeTwilioDevice() {
        try {
            this.log('Meminta token dari server...', 'info');
            this.updateStatus('Menghubungkan...', 'connecting');
            
            // Get Twilio access token from server
            const response = await fetch('/twilio-token');
            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.error || 'Gagal mendapatkan token');
            }
            
            this.log('Token diterima, membuat Twilio.Device (v2)...', 'success');
            console.log('Token:', data.token);
            
            // Check Twilio.Device constructor
            if (typeof Twilio.Device !== 'function') {
                throw new Error('Twilio.Device bukan function. SDK mungkin tidak compatible.');
            }
            
            this.log('Creating device instance...', 'info');
            
            // Initialize Twilio Device (v2 API)
            this.device = new Twilio.Device(data.token, {
                logLevel: 1, // 0=trace, 1=debug, 2=info, 3=warn, 4=error, 5=silent
                enableRingingState: true,
                codecPreferences: ['opus', 'pcmu']
            });
            
            this.log('Device instance created, setting up listeners...', 'info');
            
            // Device event listeners (v2 API)
            this.device.on('registered', () => {
                this.log('✅ Twilio Device registered!', 'success');
                this.updateStatus('Siap untuk test call', 'idle');
                this.deviceStatus.textContent = 'Terkoneksi & Siap';
                this.callBtn.disabled = false;
            });
            
            this.device.on('error', (error) => {
                this.log(`❌ Device Error: ${error.message}`, 'error');
                console.error('Device Error:', error);
                this.updateStatus('Error: ' + error.message, 'error');
                this.deviceStatus.textContent = 'Error';
            });
            
            this.device.on('incoming', (call) => {
                this.log('📞 Incoming call!', 'info');
                // For testing, we won't receive incoming calls
                // This is for outgoing calls only
            });
            
            this.device.on('unregistered', () => {
                this.log('📴 Device unregistered', 'info');
                this.deviceStatus.textContent = 'Tidak terhubung';
            });
            
            this.device.on('tokenWillExpire', () => {
                this.log('⚠️ Token akan expire, refresh token...', 'warning');
                // Implement token refresh here if needed
            });
            
            this.log('Registering device...', 'info');
            
            // Register the device
            await this.device.register();
            
            this.log('Register command sent', 'info');
            
        } catch (error) {
            this.log(`❌ Initialization Error: ${error.message}`, 'error');
            console.error('Full error:', error);
            this.updateStatus('Gagal inisialisasi: ' + error.message, 'error');
            this.deviceStatus.textContent = 'Error';
            this.callBtn.disabled = true;
        }
    }
    
    async makeCall() {
        if (!this.device) {
            this.log('❌ Device not ready', 'error');
            return;
        }
        
        try {
            this.log('📞 Making call to AI Assistant...', 'info');
            this.updateStatus('Menghubungi...', 'connecting');
            this.callBtn.disabled = true;
            
            // Make call to AI (TwiML App will handle routing)
            const params = {
                // No phone number needed - TwiML App handles routing to /twiml endpoint
            };
            
            // v2 API: connect() returns a Promise
            this.currentCall = await this.device.connect({ params });
            
            // Call event listeners (v2 API)
            this.currentCall.on('accept', (call) => {
                this.log('✅ Call accepted!', 'success');
                this.updateStatus('Panggilan terhubung', 'active');
                this.callStatus.textContent = 'Aktif';
                this.updateUI(true);
                this.startPulseAnimation();
            });
            
            this.currentCall.on('disconnect', (call) => {
                this.log('📞 Call ended', 'info');
                this.currentCall = null;
                this.updateStatus('Panggilan berakhir', 'idle');
                this.callStatus.textContent = 'Tidak ada panggilan';
                this.updateUI(false);
                this.stopPulseAnimation();
            });
            
            this.currentCall.on('error', (error) => {
                this.log(`❌ Call Error: ${error.message}`, 'error');
                this.updateStatus('Error: ' + error.message, 'error');
                this.updateUI(false);
            });
            
            this.currentCall.on('ringing', () => {
                this.log('📞 Ringing...', 'info');
                this.updateStatus('Berdering...', 'connecting');
            });
            
            this.currentCall.on('reconnecting', (error) => {
                this.log('🔄 Reconnecting...', 'warning');
            });
            
            this.currentCall.on('reconnected', () => {
                this.log('✅ Reconnected!', 'success');
            });
            
        } catch (error) {
            this.log(`❌ Make Call Error: ${error.message}`, 'error');
            this.updateStatus('Gagal membuat panggilan', 'error');
            this.callBtn.disabled = false;
        }
    }
    
    hangupCall() {
        if (this.currentCall) {
            this.log('📞 Hanging up...', 'info');
            this.currentCall.disconnect();
        }
    }
    
    toggleMute() {
        if (!this.currentCall) return;
        
        this.isMuted = !this.isMuted;
        this.currentCall.mute(this.isMuted);
        
        this.muteBtn.innerHTML = this.isMuted ? 
            '<i class="bi bi-mic-mute-fill"></i>' : 
            '<i class="bi bi-mic-fill"></i>';
        
        this.muteBtn.style.background = this.isMuted ?
            'linear-gradient(135deg, #dc3545, #c82333)' :
            'linear-gradient(135deg, #ffc107, #e0a800)';
        
        this.log(this.isMuted ? '🔇 Muted' : '🔊 Unmuted', 'info');
    }
    
    updateStatus(text, status) {
        this.statusText.textContent = text;
        this.statusBadge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
        this.statusBadge.className = `status-badge status-${status}`;
    }
    
    updateUI(callActive) {
        if (callActive) {
            this.callBtn.style.display = 'none';
            this.hangupBtn.style.display = 'flex';
            this.muteBtn.style.display = 'flex';
        } else {
            this.callBtn.style.display = 'flex';
            this.callBtn.disabled = false;
            this.hangupBtn.style.display = 'none';
            this.muteBtn.style.display = 'none';
            this.isMuted = false;
        }
    }
    
    startPulseAnimation() {
        this.callAvatar.classList.add('pulse');
    }
    
    stopPulseAnimation() {
        this.callAvatar.classList.remove('pulse');
    }
    
    log(message, type = 'info') {
        const entry = document.createElement('div');
        entry.className = `log-entry log-${type}`;
        
        const timestamp = new Date().toLocaleTimeString('id-ID');
        entry.textContent = `[${timestamp}] ${message}`;
        
        this.logsContainer.appendChild(entry);
        this.logsContainer.scrollTop = this.logsContainer.scrollHeight;
        
        console.log(`[${type.toUpperCase()}]`, message);
    }
}

// Initialize when page loads
document.addEventListener('DOMContentLoaded', () => {
    new TwilioCallTest();
});