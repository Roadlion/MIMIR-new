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
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function() {
            this.textContent = '⏳';
            fetchTickerData();
            setTimeout(() => { this.textContent = '↻ Refresh'; }, 2000);
        });
    }
    
    // Load sliding ticker data
    fetchTickerData();
    // Refresh every 5 minutes (300000ms)
    setInterval(fetchTickerData, 300000);
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
                    const priceFormatted = Number(item.current_price).toLocaleString('en-US', {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 4
                    });
                    const changeFormatted = Math.abs(item.change_percent).toFixed(2);
                    return `
                        <div class="ticker-item">
                            <span class="ticker-symbol">${item.ticker}</span>
                            <span class="ticker-price">$${priceFormatted}</span>
                            <span class="ticker-change ${changeClass}">${changeSign} ${changeFormatted}%</span>
                        </div>
                    `;
                }).join(' <span class="text-[#1A2A30]">•</span> ');
            };
            
            const listHtml = renderItems(data.tickers);
            // Render twice to support infinite marquee looping
            track.innerHTML = listHtml + ' <span class="text-[#1A2A30]">•</span> ' + listHtml;
            track.style.animationDuration = `${Math.max(25, data.tickers.length * 3.5)}s`;
            track.style.paddingLeft = '0';
        } else {
            track.innerHTML = '<span class="text-[#4A6A70]">No ticker data available</span>';
        }
    } catch (error) {
        console.error('Error loading ticker prices:', error);
        track.innerHTML = '<span class="text-[#FF5252]">Failed to load market data</span>';
    }
}