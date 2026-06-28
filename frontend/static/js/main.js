// MIMIR Frontend
console.log('🌳 MIMIR frontend loaded. The tree watches.');

function updateClock() {
    const el = document.getElementById('clock');
    if (!el) return;
    const now = new Date();
    el.textContent = now.toLocaleTimeString('en-US', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

document.addEventListener('DOMContentLoaded', function() {
    const refreshBtn = document.getElementById('refreshBtn');
    const progressPanel = document.getElementById('refreshProgressPanel');
    const stepName = document.getElementById('refreshStepName');
    const percentText = document.getElementById('refreshPercent');
    const progressBar = document.getElementById('refreshProgressBar');
    const detailMessage = document.getElementById('refreshDetailMessage');
    const logsContainer = document.getElementById('refreshLogsContainer');
    const toggleLogsBtn = document.getElementById('toggleRefreshLogsBtn');

    if (toggleLogsBtn && logsContainer) {
        toggleLogsBtn.addEventListener('click', function() {
            if (logsContainer.classList.contains('hidden')) {
                logsContainer.classList.remove('hidden');
                this.textContent = 'Hide Logs';
            } else {
                logsContainer.classList.add('hidden');
                this.textContent = 'Show Logs';
            }
        });
    }

    if (refreshBtn) {
        refreshBtn.addEventListener('click', function() {
            // Disable button
            refreshBtn.disabled = true;
            refreshBtn.classList.add('opacity-50', 'cursor-not-allowed');
            refreshBtn.textContent = '⏳ Running...';

            // Show panel
            if (progressPanel) {
                progressPanel.style.display = 'block';
                progressPanel.classList.remove('hidden');
            }

            // Reset states
            if (stepName) stepName.textContent = 'Initializing...';
            if (stepName) stepName.style.color = '#00A6B2';
            if (percentText) percentText.textContent = '0%';
            if (progressBar) {
                progressBar.style.width = '0%';
                progressBar.style.backgroundColor = '';
                progressBar.style.backgroundImage = 'linear-gradient(to right, #00A6B2, #00E5F2)';
            }
            if (detailMessage) {
                detailMessage.textContent = 'Establishing secure stream to Valhalla...';
                detailMessage.style.color = '#4A6A70';
            }
            if (logsContainer) {
                logsContainer.innerHTML = '<div class="text-[#00A6B2] mb-1">// --- MIMIR Pipeline Real-time Stream ---</div>';
            }

            // Connect SSE
            const eventSource = new EventSource('/api/v1/refresh/stream');

            eventSource.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    
                    if (data.type === 'progress') {
                        // Update UI texts
                        if (percentText) percentText.textContent = data.percentage + '%';
                        if (progressBar) progressBar.style.width = data.percentage + '%';
                        if (detailMessage) detailMessage.textContent = data.message;
                        
                        let stepLabel = 'Processing...';
                        if (data.step === 'prices') {
                            stepLabel = 'Updating Ticker Prices (yfinance)';
                        } else if (data.step === 'scraping') {
                            stepLabel = 'Scraping Articles (push_to_db.py)';
                        } else if (data.step === 'sentiment') {
                            stepLabel = 'Sentiment Pipeline (run_full_pipeline copy.py)';
                        } else if (data.step === 'done') {
                            stepLabel = 'Refresh Complete!';
                            if (detailMessage) detailMessage.style.color = '#00A6B2';
                        }
                        if (stepName) stepName.textContent = stepLabel;

                        if (data.step === 'done') {
                            eventSource.close();
                            refreshBtn.disabled = false;
                            refreshBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                            refreshBtn.textContent = '↻ Refresh';
                            // Reload page to reflect new data
                            setTimeout(() => {
                                window.location.reload();
                            }, 1500);
                        } else if (data.step === 'error') {
                            eventSource.close();
                            refreshBtn.disabled = false;
                            refreshBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                            refreshBtn.textContent = '↻ Refresh';
                            
                            if (stepName) {
                                stepName.textContent = 'Pipeline Failure';
                                stepName.style.color = '#FF5252';
                            }
                            if (detailMessage) {
                                detailMessage.textContent = data.message;
                                detailMessage.style.color = '#FF5252';
                            }
                            if (progressBar) {
                                progressBar.style.backgroundImage = 'none';
                                progressBar.style.backgroundColor = '#FF5252';
                            }
                        }
                    } else if (data.type === 'log') {
                        if (logsContainer) {
                            const lineDiv = document.createElement('div');
                            lineDiv.className = 'py-0.5 whitespace-pre-wrap border-b border-[#0A161C]/20';
                            lineDiv.textContent = data.text;
                            logsContainer.appendChild(lineDiv);
                            logsContainer.scrollTop = logsContainer.scrollHeight;
                        }
                    }
                } catch (e) {
                    console.error('Error parsing SSE event:', e);
                }
            };

            eventSource.onerror = function(err) {
                console.error('EventSource error:', err);
                eventSource.close();
                refreshBtn.disabled = false;
                refreshBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                refreshBtn.textContent = '↻ Refresh';
                
                if (stepName) {
                    stepName.textContent = 'Stream Connection Lost';
                    stepName.style.color = '#FF5252';
                }
                if (detailMessage) {
                    detailMessage.textContent = 'Connection to backend stream was interrupted.';
                    detailMessage.style.color = '#FF5252';
                }
                if (progressBar) {
                    progressBar.style.backgroundImage = 'none';
                    progressBar.style.backgroundColor = '#FF5252';
                }
            };
        });
    }
    
    // Load sliding ticker data
    fetchTickerData();
    // Refresh every 2 minutes (120000ms)
    setInterval(fetchTickerData, 120000);
});

async function fetchTickerData() {
    const track = document.getElementById('ticker-track');
    if (!track) return;
    try {
        const response = await fetch('/api/v1/prices/ticker-changes');
        if (!response.ok) throw new Error('Failed to fetch prices');
        const data = await response.json();
        
        if (data.tickers && data.tickers.length > 0) {
            const renderItems = (items) => {
                return items.map(item => {
                    const changeClass = item.change_percent >= 0 ? 'up' : 'down';
                    const changeSign = item.change_percent >= 0 ? '▲' : '▼';
                    const isCurrency = !item.ticker.startsWith('^') && !item.ticker.endsWith('=X');
                    const priceFormatted = Number(item.current_price).toLocaleString('en-US', {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 4
                    });
                    const changeFormatted = Math.abs(item.change_percent).toFixed(2);
                    return `
                        <a href="/asset/${item.ticker}" class="ticker-item hover:opacity-80 transition-opacity">
                            <span class="ticker-symbol">${item.ticker}</span>
                            <span class="ticker-price">${isCurrency ? '$' : ''}${priceFormatted}</span>
                            <span class="ticker-change ${changeClass}">${changeSign} ${changeFormatted}%</span>
                        </a>
                    `;
                }).join('');
            };
            
            const listHtml = renderItems(data.tickers);
            // Render twice to support infinite marquee looping
            track.innerHTML = listHtml + listHtml;
            track.style.animationDuration = `${Math.max(45, data.tickers.length * 6.5)}s`;
            track.style.paddingLeft = '0';
        } else {
            track.innerHTML = '<span class="text-[#4A6A70]">No ticker data available</span>';
        }
    } catch (error) {
        console.error('Error loading ticker prices:', error);
        track.innerHTML = '<span class="text-[#FF5252]">Failed to load market data</span>';
    }
}