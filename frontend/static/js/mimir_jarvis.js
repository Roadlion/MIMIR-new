/**
 * MIMIR Jarvis Voice & Portfolio Briefing Module
 */

let currentAudio = null;
let isVoiceListening = false;
let speechRecognitionInstance = null;

document.addEventListener('DOMContentLoaded', () => {
    initVoiceRecognition();
});

/**
 * Initializes Web Speech API for hands-free voice commands
 */
function initVoiceRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        console.log("Web Speech API not supported in this browser.");
        return;
    }

    speechRecognitionInstance = new SpeechRecognition();
    speechRecognitionInstance.continuous = true;
    speechRecognitionInstance.interimResults = true;
    speechRecognitionInstance.lang = 'en-US';

    speechRecognitionInstance.onresult = (event) => {
        let interimTranscript = '';
        let finalTranscript = '';

        for (let i = event.resultIndex; i < event.results.length; ++i) {
            if (event.results[i].isFinal) {
                finalTranscript += event.results[i][0].transcript;
            } else {
                interimTranscript += event.results[i][0].transcript;
            }
        }

        const displayText = (finalTranscript || interimTranscript).trim();
        if (displayText) {
            updateLiveVoiceHudText(`"${displayText}"`);
            
            // Only execute commands on the final transcript to prevent duplicate triggers
            if (finalTranscript.trim()) {
                checkAndExecuteCommand(finalTranscript.trim());
            }
        }
    };

    speechRecognitionInstance.onend = () => {
        if (isVoiceListening) {
            try {
                speechRecognitionInstance.start();
            } catch (e) {
                isVoiceListening = false;
                hideLiveVoiceHud();
                updateMicButtonUI();
            }
        } else {
            hideLiveVoiceHud();
            updateMicButtonUI();
        }
    };

    speechRecognitionInstance.onerror = (err) => {
        console.warn("Speech recognition error:", err.error);
        const errMsg = err.error === 'no-speech' ? 'No voice input detected — check Windows Mic Privacy & Volume' :
                       err.error === 'audio-capture' ? 'No microphone device found on system' :
                       err.error === 'not-allowed' ? 'Microphone permission blocked by browser' : `Mic error: ${err.error}`;
        updateLiveVoiceHudText(`⚠️ ${errMsg}`);
        showJarvisToast(`Mic Status: ${errMsg}`);
        if (err.error === 'not-allowed' || err.error === 'service-not-allowed') {
            isVoiceListening = false;
            stopMicVolumeMonitoring();
            hideLiveVoiceHud();
            updateMicButtonUI();
        }
    };
}

let micStream = null;
let audioCtx = null;
let analyserNode = null;
let animFrameId = null;

async function startMicVolumeMonitoring() {
    try {
        if (!micStream) {
            micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        }
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioCtx.state === 'suspended') {
            await audioCtx.resume();
        }
        const source = audioCtx.createMediaStreamSource(micStream);
        analyserNode = audioCtx.createAnalyser();
        analyserNode.fftSize = 64;
        source.connect(analyserNode);

        const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

        function updateVolume() {
            if (!isVoiceListening) return;
            analyserNode.getByteFrequencyData(dataArray);
            let sum = 0;
            for (let i = 0; i < dataArray.length; i++) {
                sum += dataArray[i];
            }
            let avg = sum / dataArray.length;
            let levelPercent = Math.min(100, Math.round((avg / 120) * 100));

            const bar = document.getElementById('mimirMicVolumeBar');
            const levelText = document.getElementById('mimirMicLevelText');
            if (bar) {
                bar.style.width = levelPercent + '%';
                if (levelPercent > 45) {
                    bar.className = 'h-full bg-emerald-400 transition-all duration-75';
                } else if (levelPercent > 10) {
                    bar.className = 'h-full bg-[#00A6B2] transition-all duration-75';
                } else {
                    bar.className = 'h-full bg-amber-500/50 transition-all duration-75';
                }
            }
            if (levelText) {
                if (levelPercent < 5) {
                    levelText.textContent = 'Silence (0%)';
                } else {
                    levelText.textContent = `Vol: ${levelPercent}%`;
                }
            }

            animFrameId = requestAnimationFrame(updateVolume);
        }
        updateVolume();
    } catch (e) {
        console.warn("Web Audio API volume monitor error:", e);
    }
}

function stopMicVolumeMonitoring() {
    if (animFrameId) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
    }
    if (micStream) {
        micStream.getTracks().forEach(t => t.stop());
        micStream = null;
    }
    if (audioCtx) {
        audioCtx.close().catch(() => {});
        audioCtx = null;
    }
}

let lastExecutedCmd = "";
let lastExecutedTime = 0;

function checkAndExecuteCommand(transcript) {
    if (!transcript) return false;
    const cleanCmd = transcript.toLowerCase().trim();

    // Prevent duplicate triggers within 3 seconds
    const now = Date.now();
    if (cleanCmd === lastExecutedCmd && (now - lastExecutedTime) < 3000) {
        return false;
    }

    if (cleanCmd.includes("portfolio")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Portfolio...");
        updateLiveVoiceHudText("🚀 Navigating to Portfolio...");
        setTimeout(() => { window.location.href = '/portfolio'; }, 300);
        return true;
    } else if (cleanCmd.includes("heatmap") || cleanCmd.includes("heat map") || (cleanCmd.includes("map") && cleanCmd.includes("open"))) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Heat Map...");
        updateLiveVoiceHudText("🚀 Navigating to Heat Map...");
        setTimeout(() => { window.location.href = '/map'; }, 300);
        return true;
    } else if (cleanCmd.includes("dashboard") || cleanCmd.includes("home")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Dashboard...");
        updateLiveVoiceHudText("🚀 Navigating to Dashboard...");
        setTimeout(() => { window.location.href = '/'; }, 300);
        return true;
    } else if (cleanCmd.includes("watchlist")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Watchlist...");
        updateLiveVoiceHudText("🚀 Navigating to Watchlist...");
        setTimeout(() => { window.location.href = '/watchlist'; }, 300);
        return true;
    } else if (cleanCmd.includes("alert")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Trade Alerts...");
        updateLiveVoiceHudText("🚀 Navigating to Trade Alerts...");
        setTimeout(() => { window.location.href = '/alerts'; }, 300);
        return true;
    } else if (cleanCmd.includes("intelligence") || cleanCmd.includes("article") || cleanCmd.includes("feed")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Intelligence...");
        updateLiveVoiceHudText("🚀 Navigating to Intelligence...");
        setTimeout(() => { window.location.href = '/articles'; }, 300);
        return true;
    } else if (cleanCmd.includes("oracle")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Oracle Assistant...");
        updateLiveVoiceHudText("🚀 Navigating to Oracle Assistant...");
        setTimeout(() => { window.location.href = '/oracle'; }, 300);
        return true;
    } else if (cleanCmd.includes("simulate") || cleanCmd.includes("backtest")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🚀 Navigating to Backtest Simulator...");
        updateLiveVoiceHudText("🚀 Navigating to Backtest Simulator...");
        setTimeout(() => { window.location.href = '/backtest'; }, 300);
        return true;
    } else if (cleanCmd.includes("briefing") || cleanCmd.includes("recap") || cleanCmd.includes("market briefing")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        showJarvisToast("🎙️ Opening Mimir's Briefing...");
        updateLiveVoiceHudText("🎙️ Opening Mimir's Briefing...");
        triggerMimirBriefing();
        return true;
    } else if (cleanCmd.includes("stop") || cleanCmd.includes("quiet") || cleanCmd.includes("silence") || cleanCmd.includes("mute")) {
        lastExecutedCmd = cleanCmd;
        lastExecutedTime = now;
        stopMimirAudio();
        return true;
    } else if (cleanCmd.includes("search") || cleanCmd.includes("look up") || cleanCmd.includes("find")) {
        const query = cleanCmd.replace("search", "").replace("look up", "").replace("find", "").replace("mimir", "").trim();
        if (query) {
            lastExecutedCmd = cleanCmd;
            lastExecutedTime = now;
            const searchInput = document.getElementById('searchInput');
            if (searchInput) {
                searchInput.value = query;
                searchInput.dispatchEvent(new Event('input', { bubbles: true }));
                searchInput.focus();
                showJarvisToast(`Searching for "${query}"...`);
                updateLiveVoiceHudText(`🔍 Searching for "${query}"...`);
            }
            return true;
        }
    }
    return false;
}

/**
 * Toggle hands-free mic listening
 */
async function toggleMimirVoiceMic() {
    if (!speechRecognitionInstance) {
        alert("Web Speech API is not supported in your browser. Use Chrome or Edge for voice commands!");
        return;
    }

    if (isVoiceListening) {
        isVoiceListening = false;
        speechRecognitionInstance.stop();
        stopMicVolumeMonitoring();
        hideLiveVoiceHud();
        updateMicButtonUI();
    } else {
        try {
            if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
                await navigator.mediaDevices.getUserMedia({ audio: true });
            }
            speechRecognitionInstance.start();
            isVoiceListening = true;
            showLiveVoiceHud();
            startMicVolumeMonitoring();
            updateMicButtonUI();
        } catch (e) {
            console.error("Microphone access denied or error:", e);
            showJarvisToast("Microphone permission denied by browser.");
            alert("Microphone permission was denied. Please allow microphone access in your browser URL bar lock icon!");
            isVoiceListening = false;
            stopMicVolumeMonitoring();
            hideLiveVoiceHud();
            updateMicButtonUI();
        }
    }
}

function updateMicButtonUI() {
    const micBtn = document.getElementById('mimirMicBtn');
    if (!micBtn) return;
    if (isVoiceListening) {
        micBtn.classList.add('bg-red-500/20', 'border-red-500', 'text-red-400', 'animate-pulse');
        micBtn.classList.remove('border-[#1A2A30]', 'text-[#4A6A70]');
    } else {
        micBtn.classList.remove('bg-red-500/20', 'border-red-500', 'text-red-400', 'animate-pulse');
        micBtn.classList.add('border-[#1A2A30]', 'text-[#4A6A70]');
    }
}

/**
 * Live Voice Transcript HUD UI Functions
 */
function showLiveVoiceHud() {
    let hud = document.getElementById('mimirLiveVoiceHud');
    if (!hud) {
        hud = document.createElement('div');
        hud.id = 'mimirLiveVoiceHud';
        hud.className = 'fixed top-20 left-1/2 -translate-x-1/2 z-50 bg-[#0A161C]/95 border border-[#00A6B2] backdrop-blur-md text-[#D6E5E3] px-5 py-2.5 rounded-full shadow-[0_0_25px_rgba(0,166,178,0.35)] flex items-center gap-3 font-mono text-xs transition-all duration-300 pointer-events-none opacity-0 transform -translate-y-2';
        hud.innerHTML = `
            <div class="w-2.5 h-2.5 rounded-full bg-rose-500 animate-ping"></div>
            <span class="text-[#00A6B2] font-bold tracking-wider uppercase">MIMIR Voice:</span>
            <span id="mimirLiveTranscriptText" class="italic text-[#8BA4A8]">Listening... Speak now</span>
            <div class="flex items-center gap-1.5 ml-2 pl-2 border-l border-[#1A2A30]">
                <div class="w-16 bg-[#030708] h-2 rounded overflow-hidden border border-[#1A2A30]">
                    <div id="mimirMicVolumeBar" class="h-full bg-[#00A6B2] transition-all duration-75" style="width: 0%;"></div>
                </div>
                <span id="mimirMicLevelText" class="text-[10px] text-[#4A6A70] font-mono">Vol: 0%</span>
            </div>
        `;
        document.body.appendChild(hud);
    }
    hud.classList.remove('opacity-0', '-translate-y-2');
    hud.classList.add('opacity-100', 'translate-y-0');
}

function updateLiveVoiceHudText(text) {
    showLiveVoiceHud();
    const el = document.getElementById('mimirLiveTranscriptText');
    if (el) {
        el.textContent = text;
    }
}

function hideLiveVoiceHud() {
    const hud = document.getElementById('mimirLiveVoiceHud');
    if (hud) {
        hud.classList.remove('opacity-100', 'translate-y-0');
        hud.classList.add('opacity-0', '-translate-y-2');
    }
}

/**
 * Triggers Mimir's Daily Market & Portfolio Voice Briefing
 */
async function triggerMimirBriefing() {
    const modal = document.getElementById('mimirJarvisModal');
    const loadingState = document.getElementById('jarvisLoadingState');
    const contentState = document.getElementById('jarvisContentState');
    const scriptEl = document.getElementById('jarvisVoiceScript');
    const summaryEl = document.getElementById('jarvisMarketSummary');
    const digestContainer = document.getElementById('jarvisPortfolioDigest');

    if (!modal) return;

    // Show modal & loading state
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    loadingState.classList.remove('hidden');
    contentState.classList.add('hidden');

    try {
        const cacheBuster = Date.now();
        const response = await fetch(`/api/v1/voice/recap?cb=${cacheBuster}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: Failed to generate briefing`);
        }

        const data = await response.json();
        
        // Populate script & market summary
        scriptEl.textContent = `"${data.voice_script}"`;
        summaryEl.textContent = data.market_summary || "Overnight market intelligence compiled.";

        // Populate Portfolio Digest cards
        digestContainer.innerHTML = '';
        if (data.portfolio_digest && data.portfolio_digest.length > 0) {
            data.portfolio_digest.forEach(item => {
                const statusBadge = getStatusBadgeHTML(item.status);
                const card = document.createElement('div');
                card.className = "bg-[#060A0D] border border-[#1A2A30] rounded-lg p-3 text-xs";
                card.innerHTML = `
                    <div class="flex items-center justify-between mb-1">
                        <span class="font-bold text-[#D6E5E3] font-mono text-sm">${item.ticker}</span>
                        ${statusBadge}
                    </div>
                    <p class="text-[#8B9B9E] text-xs leading-relaxed">${item.impact || "Position stable."}</p>
                `;
                digestContainer.appendChild(card);
            });
        } else {
            digestContainer.innerHTML = `
                <div class="text-xs text-[#4A6A70] italic p-3 text-center bg-[#060A0D] rounded border border-[#1A2A30]">
                    No active stock positions or major overnight sentiment anomalies detected.
                </div>
            `;
        }

        // Hide loading, show content
        loadingState.classList.add('hidden');
        contentState.classList.remove('hidden');

        // Play generated audio
        if (data.audio_url) {
            playMimirAudio(data.audio_url);
        }

    } catch (err) {
        console.error("Mimir Jarvis Error:", err);
        loadingState.classList.add('hidden');
        contentState.classList.remove('hidden');
        scriptEl.textContent = "Aye, Brother. I encountered an error fetching your market briefing from Valhalla. Check your API keys and try again!";
    }
}

function getStatusBadgeHTML(status) {
    const s = (status || '').toUpperCase();
    if (s.includes('ACCUMULAT')) {
        return `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-mono text-[10px] font-bold border border-emerald-500/30">ACCUMULATING</span>`;
    } else if (s.includes('PANIC') || s.includes('OVERSOLD')) {
        return `<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 font-mono text-[10px] font-bold border border-rose-500/30">PANIC OVERSOLD</span>`;
    } else if (s.includes('EXHAUST')) {
        return `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-400 font-mono text-[10px] font-bold border border-amber-500/30">EXHAUSTED</span>`;
    } else {
        return `<span class="px-2 py-0.5 rounded bg-[#00A6B2]/20 text-[#00A6B2] font-mono text-[10px] font-bold border border-[#00A6B2]/30">ALIGNED</span>`;
    }
}

function playMimirAudio(audioUrl) {
    stopMimirAudio();
    const cacheBuster = audioUrl.includes('?') ? '&cb=' + Date.now() : '?cb=' + Date.now();
    currentAudio = new Audio(audioUrl + cacheBuster);
    
    const waves = document.querySelectorAll('.jarvis-wave-bar');
    waves.forEach(w => w.classList.add('animate-bounce'));

    currentAudio.play().catch(e => console.warn("Audio autoplay blocked by browser:", e));

    currentAudio.onended = () => {
        waves.forEach(w => w.classList.remove('animate-bounce'));
    };
}

function stopMimirAudio() {
    if (currentAudio) {
        currentAudio.pause();
        currentAudio.currentTime = 0;
        currentAudio = null;
    }
    const waves = document.querySelectorAll('.jarvis-wave-bar');
    waves.forEach(w => w.classList.remove('animate-bounce'));
}

function closeJarvisModal() {
    stopMimirAudio();
    const modal = document.getElementById('mimirJarvisModal');
    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }
}

function showJarvisToast(msg) {
    let toast = document.getElementById('jarvisToast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'jarvisToast';
        toast.className = 'fixed bottom-5 right-5 z-50 bg-[#0A161C] border border-[#00A6B2] text-[#D6E5E3] px-4 py-2 rounded-lg text-xs shadow-2xl font-mono transition-opacity duration-300 pointer-events-none opacity-0';
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.classList.remove('opacity-0');
    toast.classList.add('opacity-100');
    setTimeout(() => {
        toast.classList.remove('opacity-100');
        toast.classList.add('opacity-0');
    }, 3500);
}
