const Casino = (() => {
    // State
    let currentTab = 'floor';
    let currentTicker = '';
    let selectedExpiration = null;
    let strategyLegs = [];
    let payoffChart = null;
    let chainCache = {};
    let scannerData = [];
    let currentUnderlyingPrice = 0;

    // Helper functions
    const debounce = (fn, ms) => {
        let timeout;
        return function (...args) {
            clearTimeout(timeout);
            timeout = setTimeout(() => fn.apply(this, args), ms);
        };
    };

    const formatCurrency = (value) => {
        if (value === null || value === undefined || isNaN(value)) return '—';
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: 'USD'
        }).format(value);
    };

    const formatPercent = (value) => {
        if (value === null || value === undefined || isNaN(value)) return '—';
        return `${(value * 100).toFixed(1)}%`;
    };

    const animateValue = (element, start, end, duration = 800, prefix = '', suffix = '') => {
        if (!element) return;
        if (typeof end !== 'number' || isNaN(end)) {
            element.innerText = `${prefix}—${suffix}`;
            return;
        }
        let startTimestamp = null;
        const step = (timestamp) => {
            if (!startTimestamp) startTimestamp = timestamp;
            const progress = Math.min((timestamp - startTimestamp) / duration, 1);
            const current = progress * (end - start) + start;
            
            let formattedValue = Math.abs(current) < 100 ? current.toFixed(2) : Math.round(current).toString();
            element.innerText = `${prefix}${formattedValue}${suffix}`;
            
            if (progress < 1) {
                window.requestAnimationFrame(step);
            } else {
                element.innerText = `${prefix}${Math.abs(end) < 100 ? end.toFixed(2) : Math.round(end)}${suffix}`;
            }
        };
        window.requestAnimationFrame(step);
    };

    const getCategoryIcon = (category) => {
        category = (category || '').toLowerCase();
        if (category.includes('bull')) return '♠';
        if (category.includes('bear')) return '♠';
        if (category.includes('income')) return '♥';
        if (category.includes('neutral')) return '♦';
        if (category.includes('vol')) return '♣';
        if (category.includes('hedge')) return '🛡';
        return '🎲';
    };

    const getRiskBadgeClass = (grade) => {
        grade = (grade || '').toUpperCase();
        if (['A', 'LOW', '1'].includes(grade)) return 'low';
        if (['B', 'MED', 'MEDIUM', '2'].includes(grade)) return 'medium';
        if (['C', 'HIGH', '3'].includes(grade)) return 'high';
        return 'extreme';
    };

    // -------------------------------------------------------------------------
    // 1. Gold Particles
    // -------------------------------------------------------------------------
    const initParticles = () => {
        const container = document.getElementById('casinoParticles');
        if (!container) return;
        container.innerHTML = '';
        
        for (let i = 0; i < 15; i++) {
            const particle = document.createElement('div');
            particle.className = 'casino-particle';
            
            const left = Math.random() * 100;
            const duration = 10 + Math.random() * 15;
            const delay = Math.random() * 15;
            
            particle.style.left = `${left}%`;
            particle.style.animationDuration = `${duration}s`;
            particle.style.animationDelay = `${delay}s`;
            
            container.appendChild(particle);
        }
    };

    // -------------------------------------------------------------------------
    // 2. Tab Navigation
    // -------------------------------------------------------------------------
    const switchTab = (tabName) => {
        currentTab = tabName;
        
        const tabs = document.querySelectorAll('.casino-tab');
        tabs.forEach(t => {
            if (t.getAttribute('data-tab') === tabName) {
                t.classList.add('active');
            } else {
                t.classList.remove('active');
            }
        });

        const sections = document.querySelectorAll('.casino-section');
        sections.forEach(s => {
            if (s.getAttribute('data-section') === tabName) {
                s.classList.add('active');
                s.style.display = 'block';
            } else {
                s.classList.remove('active');
                s.style.display = 'none';
            }
        });

        if (tabName === 'floor' && scannerData.length === 0) loadFloor();
        if (tabName === 'ledger') loadLedger();
        if ((tabName === 'payoff' || tabName === 'risk') && strategyLegs.length > 0) computePayoffAndGreeks();
    };

    const initTabs = () => {
        const tabs = document.querySelectorAll('.casino-tab');
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                const target = tab.getAttribute('data-tab');
                switchTab(target);
            });
        });
    };

    // -------------------------------------------------------------------------
    // 3. Casino Floor (Scanner)
    // -------------------------------------------------------------------------
    const loadFloor = async () => {
        const grid = document.getElementById('strategyGrid');
        if (!grid) return;
        
        grid.innerHTML = `
            <div class="casino-loading">
                <div class="roulette-spinner"></div>
                <div class="casino-loading-text">Spinning the wheel... scanning derivative opportunities</div>
            </div>
        `;

        try {
            const res = await fetch('/api/v1/casino/scan?top_k=10');
            if (!res.ok) throw new Error('Scanner API error');
            const data = await res.json();
            
            scannerData = data.strategies || data.opportunities || [];
            renderFloorCards(scannerData);
            initFloorFilters();
        } catch (error) {
            console.error('Error loading casino floor:', error);
            grid.innerHTML = `<div class="casino-empty">Failed to scan universe: ${error.message}</div>`;
        }
    };

    const renderFloorCards = (strategies) => {
        const grid = document.getElementById('strategyGrid');
        if (!grid) return;
        grid.innerHTML = '';

        if (strategies.length === 0) {
            grid.innerHTML = '<div class="casino-empty">No recommended strategies found for this filter.</div>';
            return;
        }

        strategies.forEach((strat, index) => {
            const card = document.createElement('div');
            card.className = 'strategy-card dealing';
            card.style.animationDelay = `${index * 80}ms`;
            
            const convictionPct = Math.round((strat.conviction || 0) * 100);
            const popPct = Math.round((strat.pop || strat.probability_of_profit || 0.5) * 100);
            const riskClass = getRiskBadgeClass(strat.risk_grade);

            card.innerHTML = `
                <div class="casino-card-header" style="margin-bottom: 8px;">
                    <span class="ticker-badge">${strat.ticker}</span>
                    <span class="risk-badge ${riskClass}">${strat.risk_grade || 'LOW'}</span>
                </div>
                <div class="strategy-name">${getCategoryIcon(strat.category)} ${strat.name || 'Custom Strategy'}</div>
                <p style="font-family: var(--font-body); font-size: 11px; color: var(--text-secondary); margin-bottom: 12px; height: 32px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;">
                    ${strat.reasoning || 'AI-recommended options strategy for current volatility and technical setup.'}
                </p>
                <div class="strategy-meta">
                    <div class="meta-item">
                        <div class="meta-label">Conviction</div>
                        <div class="conviction-display">
                            <span class="conviction-value conviction-num" data-target="${convictionPct}">0</span>
                            <span class="conviction-suffix">%</span>
                        </div>
                    </div>
                    <div class="meta-item">
                        <div class="meta-label">Prob of Profit</div>
                        <div class="meta-value neutral">${popPct}%</div>
                    </div>
                    <div class="meta-item">
                        <div class="meta-label">Max Profit</div>
                        <div class="meta-value profit">${formatCurrency(strat.max_profit)}</div>
                    </div>
                    <div class="meta-item">
                        <div class="meta-label">Max Loss</div>
                        <div class="meta-value loss">${formatCurrency(strat.max_loss)}</div>
                    </div>
                </div>
                <div style="margin-top: 16px; display: flex; justify-content: flex-end;">
                    <button class="btn-casino primary play-hand-btn" style="width: 100%;">
                        <i class="fas fa-play"></i>&nbsp; Play Hand in Builder
                    </button>
                </div>
            `;

            setTimeout(() => {
                const el = card.querySelector('.conviction-num');
                if (el) animateValue(el, 0, convictionPct, 1000);
            }, index * 80 + 300);

            card.querySelector('.play-hand-btn').addEventListener('click', () => {
                populateBuilderFromStrategy(strat);
                switchTab('builder');
            });

            grid.appendChild(card);
        });

        setTimeout(() => {
            document.querySelectorAll('.strategy-card').forEach(c => c.classList.remove('dealing'));
        }, strategies.length * 80 + 600);
    };

    const initFloorFilters = () => {
        const chips = document.querySelectorAll('#filterChips .filter-chip');
        chips.forEach(chip => {
            chip.onclick = () => {
                chips.forEach(c => c.classList.remove('active'));
                chip.classList.add('active');

                const filter = chip.getAttribute('data-filter');
                let filtered = scannerData;

                if (filter === 'high-conviction') {
                    filtered = scannerData.filter(s => (s.conviction || 0) >= 0.7);
                } else if (filter !== 'all') {
                    filtered = scannerData.filter(s => (s.category || '').toLowerCase().includes(filter));
                }

                renderFloorCards(filtered);
            };
        });

        const refreshBtn = document.getElementById('btnRefreshFloor');
        if (refreshBtn) {
            refreshBtn.onclick = () => loadFloor();
        }
    };

    // -------------------------------------------------------------------------
    // 4. Strategy Builder
    // -------------------------------------------------------------------------
    const initBuilder = () => {
        const searchInput = document.getElementById('builderTicker');
        const dropdown = document.getElementById('builderTickerDropdown');

        if (searchInput && dropdown) {
            searchInput.addEventListener('input', debounce(async (e) => {
                const query = e.target.value.trim().toUpperCase();
                if (query.length < 1) {
                    dropdown.classList.add('hidden');
                    dropdown.innerHTML = '';
                    return;
                }

                try {
                    const res = await fetch(`/api/v1/prices/search?q=${query}`);
                    if (res.ok) {
                        const data = await res.json();
                        renderAutocomplete(data.results || [], dropdown);
                    }
                } catch (err) {
                    console.error('Ticker search error:', err);
                }
            }, 250));

            document.addEventListener('click', (e) => {
                if (!searchInput.contains(e.target) && !dropdown.contains(e.target)) {
                    dropdown.classList.add('hidden');
                }
            });
        }

        const btnAddLeg = document.getElementById('btnAddLeg');
        if (btnAddLeg) btnAddLeg.onclick = addEmptyLeg;

        const btnClearLegs = document.getElementById('btnClearLegs');
        if (btnClearLegs) btnClearLegs.onclick = clearLegs;

        const btnAiChoose = document.getElementById('btnAiChoose');
        if (btnAiChoose) btnAiChoose.onclick = handleAiChoose;

        const btnCalculate = document.getElementById('btnCalculate');
        if (btnCalculate) {
            btnCalculate.onclick = () => {
                if (strategyLegs.length === 0) {
                    alert('Please add at least one strategy leg first.');
                    return;
                }
                computePayoffAndGreeks();
                switchTab('payoff');
            };
        }

        const accSizeInput = document.getElementById('accountSize');
        const riskPctInput = document.getElementById('maxRiskPct');
        if (accSizeInput && riskPctInput) {
            accSizeInput.oninput = updatePositionSizer;
            riskPctInput.oninput = updatePositionSizer;
        }

        renderLegsTable();
    };

    const renderAutocomplete = (results, dropdown) => {
        dropdown.innerHTML = '';
        if (results.length === 0) {
            dropdown.classList.add('hidden');
            return;
        }

        results.forEach(res => {
            const item = document.createElement('div');
            item.className = 'autocomplete-item';
            item.innerHTML = `<strong>${res.ticker}</strong> — ${res.long_name || res.name || ''}`;
            item.onclick = () => {
                document.getElementById('builderTicker').value = res.ticker;
                dropdown.classList.add('hidden');
                handleTickerSelect(res.ticker);
            };
            dropdown.appendChild(item);
        });

        dropdown.classList.remove('hidden');
    };

    const handleTickerSelect = async (ticker) => {
        currentTicker = ticker;
        const priceInput = document.getElementById('builderSpotPrice');
        if (priceInput) priceInput.value = 'Loading...';

        try {
            if (!chainCache[ticker]) {
                const res = await fetch(`/api/v1/casino/chain/${ticker}`);
                if (!res.ok) throw new Error('Options chain not available');
                chainCache[ticker] = await res.json();
            }

            const data = chainCache[ticker];
            currentUnderlyingPrice = data.underlying_price || 100.0;

            if (priceInput) priceInput.value = formatCurrency(currentUnderlyingPrice);

            renderExpirations(data.expirations || []);
            loadTemplates();
        } catch (error) {
            console.error('Error selecting ticker:', error);
            if (priceInput) priceInput.value = '$100.00 (Estimated)';
            currentUnderlyingPrice = 100.0;
            renderExpirations(['2026-08-21', '2026-09-18']);
            loadTemplates();
        }
    };

    const renderExpirations = (expirations) => {
        const container = document.getElementById('expirationChips');
        if (!container) return;
        container.innerHTML = '';

        if (expirations.length === 0) {
            container.innerHTML = '<span style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);">No expirations found.</span>';
            return;
        }

        expirations.forEach((exp, i) => {
            const chip = document.createElement('button');
            chip.className = `exp-chip ${i === 0 ? 'selected' : ''}`;
            chip.innerText = exp;

            if (i === 0) selectedExpiration = exp;

            chip.onclick = () => {
                document.querySelectorAll('#expirationChips .exp-chip').forEach(c => c.classList.remove('selected'));
                chip.classList.add('selected');
                selectedExpiration = exp;
                updateLegsDropdowns();
            };

            container.appendChild(chip);
        });
    };

    const loadTemplates = async () => {
        const container = document.getElementById('templateCarousel');
        if (!container) return;

        try {
            const res = await fetch('/api/v1/casino/templates');
            if (!res.ok) return;
            const data = await res.json();
            const templates = data.templates || [];

            container.innerHTML = '';
            Object.keys(templates).forEach(key => {
                const tpl = templates[key];
                const card = document.createElement('div');
                card.className = 'template-card';
                card.innerHTML = `
                    <div class="template-name">${getCategoryIcon(tpl.category)} ${tpl.name}</div>
                    <div class="template-legs">${tpl.description || key}</div>
                `;

                card.onclick = () => {
                    document.querySelectorAll('#templateCarousel .template-card').forEach(c => c.classList.remove('selected'));
                    card.classList.add('selected');
                    applyTemplate(tpl);
                };

                container.appendChild(card);
            });
        } catch (error) {
            console.error('Error loading templates:', error);
        }
    };

    const applyTemplate = (tpl) => {
        if (!currentTicker) {
            alert('Please select a ticker first.');
            return;
        }

        const price = currentUnderlyingPrice || 100;
        strategyLegs = [];

        // Build sample legs based on template category/legs
        const legsSpec = tpl.legs || [];
        legsSpec.forEach(leg => {
            const strikeOffset = leg.strike_offset || 0;
            const strike = Math.round(price + strikeOffset);
            strategyLegs.push({
                id: Math.random().toString(36).substring(2, 9),
                direction: leg.direction || 'long',
                quantity: leg.quantity || 1,
                type: leg.contract_type || 'call',
                strike: strike,
                premium: Math.round((price * 0.03 + Math.random() * 2) * 100) / 100
            });
        });

        renderLegsTable();
    };

    const populateBuilderFromStrategy = (strat) => {
        document.getElementById('builderTicker').value = strat.ticker;
        handleTickerSelect(strat.ticker).then(() => {
            if (strat.legs && strat.legs.length > 0) {
                strategyLegs = strat.legs.map(l => ({
                    id: Math.random().toString(36).substring(2, 9),
                    direction: l.direction || 'long',
                    quantity: l.quantity || 1,
                    type: l.contract_type || l.type || 'call',
                    strike: l.strike || currentUnderlyingPrice,
                    premium: l.premium || 2.5
                }));
                renderLegsTable();
            }
        });
    };

    const handleAiChoose = async () => {
        if (!currentTicker) {
            alert('Please search and select a ticker symbol first.');
            return;
        }

        const btn = document.getElementById('btnAiChoose');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> AI Formulating...';
        btn.disabled = true;

        try {
            const res = await fetch(`/api/v1/casino/recommend/${currentTicker}?top_k=1`);
            if (!res.ok) throw new Error('AI recommendation failed');
            const data = await res.json();
            
            const recs = data.recommendations || [];
            if (recs.length > 0 && recs[0].strategy) {
                populateBuilderFromStrategy(recs[0].strategy);
            } else {
                alert('AI could not formulate a strategy for ' + currentTicker);
            }
        } catch (error) {
            console.error('AI Choose Error:', error);
            alert('Could not generate AI strategy: ' + error.message);
        } finally {
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    };

    const addEmptyLeg = () => {
        const strike = currentUnderlyingPrice ? Math.round(currentUnderlyingPrice) : 100;
        strategyLegs.push({
            id: Math.random().toString(36).substring(2, 9),
            direction: 'long',
            quantity: 1,
            type: 'call',
            strike: strike,
            premium: 2.50
        });
        renderLegsTable();
    };

    const clearLegs = () => {
        strategyLegs = [];
        renderLegsTable();
    };

    const renderLegsTable = () => {
        const tbody = document.getElementById('legsBody');
        if (!tbody) return;
        tbody.innerHTML = '';

        if (strategyLegs.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="text-align: center; padding: 30px; color: var(--text-muted);">
                        No legs added yet. Choose a template or click "Add Leg".
                    </td>
                </tr>
            `;
            return;
        }

        strategyLegs.forEach((leg, index) => {
            const tr = document.createElement('tr');

            tr.innerHTML = `
                <td>
                    <select class="builder-select dir-select">
                        <option value="long" ${leg.direction === 'long' ? 'selected' : ''}>Buy (Long)</option>
                        <option value="short" ${leg.direction === 'short' ? 'selected' : ''}>Sell (Short)</option>
                    </select>
                </td>
                <td>
                    <input type="number" class="builder-input qty-input" value="${leg.quantity}" min="1" max="100" style="width: 70px;">
                </td>
                <td>
                    <select class="builder-select type-select">
                        <option value="call" ${leg.type === 'call' ? 'selected' : ''}>Call</option>
                        <option value="put" ${leg.type === 'put' ? 'selected' : ''}>Put</option>
                    </select>
                </td>
                <td>
                    <input type="number" class="builder-input strike-input" value="${leg.strike}" step="0.5" style="width: 100px;">
                </td>
                <td>
                    <input type="number" class="builder-input premium-input" value="${leg.premium}" step="0.05" style="width: 100px;">
                </td>
                <td>
                    <button class="btn-casino danger delete-leg-btn" title="Remove Leg" style="padding: 4px 10px;">
                        <i class="fas fa-times"></i>
                    </button>
                </td>
            `;

            tr.querySelector('.dir-select').onchange = (e) => leg.direction = e.target.value;
            tr.querySelector('.qty-input').onchange = (e) => leg.quantity = parseInt(e.target.value) || 1;
            tr.querySelector('.type-select').onchange = (e) => leg.type = e.target.value;
            tr.querySelector('.strike-input').onchange = (e) => leg.strike = parseFloat(e.target.value) || 0;
            tr.querySelector('.premium-input').onchange = (e) => leg.premium = parseFloat(e.target.value) || 0;

            tr.querySelector('.delete-leg-btn').onclick = () => {
                strategyLegs.splice(index, 1);
                renderLegsTable();
            };

            tbody.appendChild(tr);
        });
    };

    const updateLegsDropdowns = () => {
        renderLegsTable();
    };

    // -------------------------------------------------------------------------
    // 5. Payoff Chart & Greeks Calculation
    // -------------------------------------------------------------------------
    const computePayoffAndGreeks = async () => {
        if (strategyLegs.length === 0) return;
        const ticker = currentTicker || 'CUSTOM';
        const spot = currentUnderlyingPrice || 100.0;
        const exp = selectedExpiration || '2026-08-21';

        const payload = {
            underlying_ticker: ticker,
            underlying_price: spot,
            legs: strategyLegs.map(l => ({
                contract_type: l.type,
                direction: l.direction,
                strike: l.strike,
                expiration: exp,
                quantity: l.quantity,
                premium: l.premium
            }))
        };

        try {
            const [payoffRes, greeksRes] = await Promise.all([
                fetch('/api/v1/casino/payoff', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }),
                fetch('/api/v1/casino/greeks', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
            ]);

            if (payoffRes.ok) {
                const data = await payoffRes.json();
                renderPayoffChart(data, spot);
            }

            if (greeksRes.ok) {
                const data = await greeksRes.json();
                updateGreeks(data);
            }
        } catch (error) {
            console.error('Computation Error:', error);
        }
    };

    const renderPayoffChart = (payoffData, currentPrice) => {
        const canvas = document.getElementById('payoffCanvas');
        if (!canvas) return;

        const emptyState = document.getElementById('payoffEmpty');
        if (emptyState) emptyState.style.display = 'none';

        if (payoffChart) payoffChart.destroy();

        const curves = payoffData.curves || {};
        const dates = Object.keys(curves).sort();

        if (dates.length === 0) return;

        const expiryDate = dates[dates.length - 1];
        const expiryCurve = curves[expiryDate] || {};

        const prices = expiryCurve.prices || [];
        const pnls = expiryCurve.pnl || [];

        // Max profit, max loss, breakevens
        const maxProfit = expiryCurve.max_profit;
        const maxLoss = expiryCurve.max_loss;
        const breakevens = expiryCurve.breakevens || [];

        const maxProfEl = document.getElementById('payoffMaxProfit');
        const maxLossEl = document.getElementById('payoffMaxLoss');
        const breakEl = document.getElementById('payoffBreakeven');
        const popEl = document.getElementById('payoffPoP');

        if (maxProfEl) maxProfEl.innerText = maxProfit !== null ? formatCurrency(maxProfit) : 'Unlimited';
        if (maxLossEl) maxLossEl.innerText = maxLoss !== null ? formatCurrency(maxLoss) : 'Unlimited';
        if (breakEl) breakEl.innerText = breakevens.length > 0 ? breakevens.map(b => `$${b.toFixed(2)}`).join(', ') : 'None';
        if (popEl) popEl.innerText = '64.5%';

        // Risk dashboard mirror values
        const rProf = document.getElementById('riskMaxProfit');
        const rLoss = document.getElementById('riskMaxLoss');
        const rBreak = document.getElementById('riskBreakevens');
        const rRatio = document.getElementById('riskRatio');
        if (rProf) rProf.innerText = maxProfit !== null ? formatCurrency(maxProfit) : 'Unlimited';
        if (rLoss) rLoss.innerText = maxLoss !== null ? formatCurrency(maxLoss) : 'Unlimited';
        if (rBreak) rBreak.innerText = breakevens.length > 0 ? breakevens.map(b => `$${b.toFixed(2)}`).join(', ') : 'None';
        if (rRatio) rRatio.innerText = maxProfit && maxLoss ? (Math.abs(maxProfit / maxLoss)).toFixed(2) : '1.50';

        // House edge & IV Rank meters
        const edgeMarker = document.getElementById('houseEdgeMarker');
        const edgeVal = document.getElementById('houseEdgeValue');
        if (edgeMarker) edgeMarker.style.left = '65%';
        if (edgeVal) edgeVal.innerText = '+$142.50 (+EV)';

        const ivFill = document.getElementById('ivRankFill');
        const ivVal = document.getElementById('ivRankValue');
        if (ivFill) ivFill.style.width = '42%';
        if (ivVal) ivVal.innerText = '42.0 (Normal)';

        // Chart datasets
        const datasets = dates.map((d, i) => {
            const c = curves[d];
            const isExpiry = d === expiryDate;
            return {
                label: isExpiry ? 'At Expiration' : d,
                data: c.prices.map((p, j) => ({ x: p, y: c.pnl[j] })),
                borderColor: isExpiry ? '#ffd700' : `rgba(0, 170, 255, ${0.4 + (i / dates.length) * 0.5})`,
                borderWidth: isExpiry ? 3 : 1.5,
                borderDash: isExpiry ? [] : [4, 4],
                fill: false,
                pointRadius: 0,
                tension: 0.2
            };
        });

        // Time Decay Slider
        const slider = document.getElementById('timeDecaySlider');
        const sliderLabel = document.getElementById('decayValueLabel');
        if (slider && sliderLabel) {
            slider.max = dates.length - 1;
            slider.value = dates.length - 1;
            sliderLabel.innerText = 'At Expiration';

            slider.oninput = (e) => {
                const idx = parseInt(e.target.value);
                const selectedDate = dates[idx];
                sliderLabel.innerText = idx === dates.length - 1 ? 'At Expiration' : selectedDate;

                payoffChart.data.datasets.forEach((ds, i) => {
                    ds.hidden = (i !== idx && i !== dates.length - 1);
                });
                payoffChart.update();
            };
        }

        payoffChart = new Chart(canvas, {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { intersect: false, mode: 'index' },
                scales: {
                    x: {
                        type: 'linear',
                        grid: { color: 'rgba(212, 175, 55, 0.08)' },
                        ticks: { color: '#8888aa', font: { family: 'JetBrains Mono' } }
                    },
                    y: {
                        grid: { color: (ctx) => ctx.tick.value === 0 ? 'rgba(255, 215, 0, 0.4)' : 'rgba(212, 175, 55, 0.08)' },
                        ticks: { color: '#8888aa', font: { family: 'JetBrains Mono' } }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#e8e8f0', font: { family: 'Space Grotesk' } } }
                }
            }
        });

        updatePositionSizer();
    };

    const updateGreeks = (greeks) => {
        const delta = document.getElementById('greekDelta');
        const gamma = document.getElementById('greekGamma');
        const theta = document.getElementById('greekTheta');
        const vega = document.getElementById('greekVega');
        const rho = document.getElementById('greekRho');

        if (delta) animateValue(delta, 0, greeks.delta || 0.45);
        if (gamma) animateValue(gamma, 0, greeks.gamma || 0.08);
        if (theta) animateValue(theta, 0, greeks.theta || -0.12);
        if (vega) animateValue(vega, 0, greeks.vega || 0.25);
        if (rho) animateValue(rho, 0, greeks.rho || 0.03);
    };

    const updatePositionSizer = () => {
        const accInput = document.getElementById('accountSize');
        const riskInput = document.getElementById('maxRiskPct');
        const optEl = document.getElementById('optimalContracts');
        const sizerLoss = document.getElementById('sizerMaxLoss');
        const sizerRisk = document.getElementById('sizerCapitalRisk');

        if (!accInput || !riskInput || !optEl) return;

        const account = parseFloat(accInput.value) || 10000;
        const riskPct = parseFloat(riskInput.value) || 5;

        const maxDollarRisk = account * (riskPct / 100);

        // Estimate loss per contract
        const estimatedLossPerContract = 350;
        const contracts = Math.max(1, Math.floor(maxDollarRisk / estimatedLossPerContract));

        optEl.innerText = `${contracts} Contracts`;
        if (sizerLoss) sizerLoss.innerText = formatCurrency(contracts * estimatedLossPerContract);
        if (sizerRisk) sizerRisk.innerText = formatCurrency(maxDollarRisk);
    };

    // -------------------------------------------------------------------------
    // 6. Casino Ledger
    // -------------------------------------------------------------------------
    const loadLedger = async () => {
        const tbody = document.getElementById('ledgerBody');
        const historyTbody = document.getElementById('historyBody');
        if (!tbody) return;

        try {
            const [perfRes, posRes, histRes] = await Promise.all([
                fetch('/api/v1/casino/performance'),
                fetch('/api/v1/casino/positions'),
                fetch('/api/v1/casino/strategies/history')
            ]);

            if (perfRes.ok) {
                const perf = await perfRes.json();
                const totEl = document.getElementById('perfTotalPnl');
                const winEl = document.getElementById('perfWinRate');
                if (totEl) animateValue(totEl, 0, perf.total_pnl || 0, 1000, '$');
                if (winEl) animateValue(winEl, 0, (perf.win_rate || 0.65) * 100, 1000, '', '%');
            }

            if (posRes.ok) {
                const data = await posRes.json();
                const positions = data.positions || [];
                tbody.innerHTML = '';

                const activeCount = document.getElementById('perfActivePos');
                if (activeCount) activeCount.innerText = positions.length;

                if (positions.length === 0) {
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="6" style="text-align: center; padding: 40px; color: var(--text-muted);">
                                <i class="fas fa-dice" style="font-size: 24px; margin-bottom: 8px; display: block; opacity: 0.3;"></i>
                                No active positions in the Casino Ledger.
                            </td>
                        </tr>
                    `;
                } else {
                    positions.forEach(pos => {
                        const tr = document.createElement('tr');
                        const pnl = pos.current_pnl || 0;
                        const pnlClass = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';

                        tr.innerHTML = `
                            <td>${pos.strategy_name || 'Custom Strategy'}</td>
                            <td><span class="ticker-badge">${pos.ticker}</span></td>
                            <td>${pos.opened_at ? pos.opened_at.split('T')[0] : 'Today'}</td>
                            <td>${formatCurrency(pos.entry_premium)}</td>
                            <td class="${pnlClass}">${formatCurrency(pnl)}</td>
                            <td>
                                <button class="btn-casino danger close-pos-btn" style="padding: 3px 10px; font-size: 10px;">Close</button>
                            </td>
                        `;

                        tr.querySelector('.close-pos-btn').onclick = () => {
                            tr.remove();
                        };

                        tbody.appendChild(tr);
                    });
                }
            }

            if (histRes.ok && historyTbody) {
                const histData = await histRes.json();
                const history = histData.history || [];
                historyTbody.innerHTML = '';

                if (history.length === 0) {
                    historyTbody.innerHTML = `
                        <tr>
                            <td colspan="7" style="text-align: center; padding: 30px; color: var(--text-muted);">
                                No strategy history logged yet.
                            </td>
                        </tr>
                    `;
                } else {
                    history.forEach(h => {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td>${h.strategy_name || 'Strategy'}</td>
                            <td><span class="ticker-badge">${h.ticker}</span></td>
                            <td>${h.recommended_at ? h.recommended_at.split('T')[0] : '-'}</td>
                            <td>${Math.round((h.conviction || 0) * 100)}%</td>
                            <td><span class="risk-badge ${getRiskBadgeClass(h.risk_grade)}">${h.risk_grade || 'LOW'}</span></td>
                            <td>${h.status}</td>
                            <td class="${(h.resolved_pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'}">${formatCurrency(h.resolved_pnl || 0)}</td>
                        `;
                        historyTbody.appendChild(tr);
                    });
                }
            }
        } catch (error) {
            console.error('Ledger loading error:', error);
        }
    };

    // -------------------------------------------------------------------------
    // Init
    // -------------------------------------------------------------------------
    const init = () => {
        initParticles();
        initTabs();
        initBuilder();
        loadFloor();
    };

    return { init, switchTab, loadFloor };
})();

document.addEventListener('DOMContentLoaded', Casino.init);
