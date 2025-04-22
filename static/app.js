// static/app.js (v9.1 - Simplified, No User Management)

document.addEventListener('DOMContentLoaded', function() {
    // --- Initialize Bootstrap Tooltips ---
    function initializeTooltips() {
        var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
        tooltipTriggerList.forEach(function (tooltipTriggerEl) {
            var existingTooltip = bootstrap.Tooltip.getInstance(tooltipTriggerEl);
            if (existingTooltip) { existingTooltip.dispose(); }
            new bootstrap.Tooltip(tooltipTriggerEl);
        });
    }
    initializeTooltips();

    // --- Settings Modal Code ---
    const settingsModal = document.getElementById('settingsModal');
    const retentionSlider = document.getElementById('retentionSlider');
    const retentionValueDisplay = document.getElementById('retentionValue');
    const saveSettingsBtn = document.getElementById('saveSettingsBtn');
    const settingsForm = document.getElementById('settingsForm');
    const settingsAlert = document.getElementById('settingsAlert');
    const settingsAlertMessage = document.getElementById('settingsAlertMessage');
    const saveSettingsSpinner = saveSettingsBtn ? saveSettingsBtn.querySelector('.spinner-border') : null;

    if (retentionSlider && retentionValueDisplay) {
        retentionValueDisplay.textContent = retentionSlider.value;
        retentionSlider.addEventListener('input', function() { retentionValueDisplay.textContent = this.value; });
    }
    if (settingsForm && saveSettingsBtn && saveSettingsSpinner) {
        settingsForm.addEventListener('submit', function(event) {
            event.preventDefault(); const formData = new FormData(settingsForm); const newRetention = formData.get('max_alerts_retention');
            saveSettingsBtn.disabled = true; saveSettingsSpinner.style.display = 'inline-block'; settingsAlert.style.display = 'none';
            fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, body: JSON.stringify({ max_alerts_retention: parseInt(newRetention, 10) }) })
            .then(response => { if (!response.ok) { return response.json().then(err => { throw new Error(err.message || `Error ${response.status}`); }).catch(() => { throw new Error(`Error ${response.status}`); }); } return response.json(); })
            .then(data => { settingsAlertMessage.textContent = data.message || 'Settings saved!'; settingsAlert.className = 'alert alert-success alert-dismissible fade show'; settingsAlert.style.display = 'block'; })
            .catch((error) => { console.error('Error saving settings:', error); settingsAlertMessage.textContent = `Error: ${error.message}.`; settingsAlert.className = 'alert alert-danger alert-dismissible fade show'; settingsAlert.style.display = 'block'; })
            .finally(() => { saveSettingsBtn.disabled = false; saveSettingsSpinner.style.display = 'none'; });
        });
    }
     if (settingsAlert) {
         const alertInstance = new bootstrap.Alert(settingsAlert);
         settingsAlert.addEventListener('close.bs.alert', () => { settingsAlert.style.display = 'none'; });
     }

    // --- Alert Notification Logic ---
    const notificationSound = document.getElementById('notificationSound');
    const latestTimestampSpan = document.getElementById('latest-alert-timestamp');
    const enableNotificationsBtn = document.getElementById('enableNotificationsBtn');
    const pageTitleElement = document.getElementById('pageTitle');
    const originalPageTitle = pageTitleElement ? pageTitleElement.innerText : "Alert Bord"; // Updated Name
    const newAlertIndicator = document.getElementById('new-alert-indicator');
    let lastKnownTimestamp = latestTimestampSpan ? latestTimestampSpan.dataset.timestamp : null;
    let notificationPermission = 'Notification' in window ? Notification.permission : 'denied';
    let pollingInterval = 15000; let errorCount = 0; const maxErrors = 5; let pollTimeoutId = null; let newAlertsReceived = false; let titleUpdateInterval = null;
    function requestNotificationPermission() {
        if (!('Notification' in window)) return;
        if (Notification.permission === 'default') { Notification.requestPermission().then(permission => { notificationPermission = permission; if (enableNotificationsBtn) enableNotificationsBtn.style.display = 'none'; if (permission === 'granted') { try { new Notification('Alert Bord', { body: 'Notifications enabled!', tag: 'permission-info' }); } catch(e){ console.error(e); } } }); } else if (enableNotificationsBtn) { enableNotificationsBtn.style.display = 'none'; }
    }
    function playNotificationSound() { if (notificationSound) { notificationSound.currentTime = 0; notificationSound.play().catch(e => { console.warn("Audio play failed.", e); }); } }
    function showNotification(alert) { if (notificationPermission !== 'granted') return; const name = alert.alertname || alert.alertmanager_labels?.alertname || 'Alert'; const sev = alert.severity?.toUpperCase() || 'UNK'; const grp = alert.alert_group || 'Def'; let msg = alert.message || ''; if (msg.startsWith(`[${name}]`)) { msg = msg.substring(`[${name}]`.length).trim(); } const title = `[${sev}] ${name}`; const opts = { body: `Grp: ${grp}\n${msg.substring(0, 150)}${msg.length > 150 ? '...' : ''}`, tag: alert.id || `alert-${Date.now()}` }; try { const n = new Notification(title, opts); n.onclick = (e) => { window.focus(); e.target.close(); }; } catch (err) { console.error("Notification error:", err); } }
    function updateTitleIndicator() { if (!pageTitleElement) return; if (newAlertsReceived) { if (!titleUpdateInterval) { let show = true; titleUpdateInterval = setInterval(() => { pageTitleElement.innerText = show ? `(!!) ${originalPageTitle}` : originalPageTitle; show = !show; }, 1000); } if (newAlertIndicator) newAlertIndicator.style.display = 'inline-block'; } else { if (titleUpdateInterval) { clearInterval(titleUpdateInterval); titleUpdateInterval = null; } pageTitleElement.innerText = originalPageTitle; if (newAlertIndicator) newAlertIndicator.style.display = 'none'; } }
    function clearNewAlertsIndicator() { if (newAlertsReceived) { newAlertsReceived = false; updateTitleIndicator(); } }
    document.body.addEventListener('click', clearNewAlertsIndicator); document.body.addEventListener('change', clearNewAlertsIndicator); document.body.addEventListener('submit', clearNewAlertsIndicator);
    function pollForNewAlerts() {
        const timestampToSend = latestTimestampSpan ? latestTimestampSpan.dataset.timestamp : ""; console.debug(`Polling since: '${timestampToSend}'`); if (pollTimeoutId) clearTimeout(pollTimeoutId);
        fetch(`/api/new_alerts?since_timestamp=${encodeURIComponent(timestampToSend)}`)
        .then(response => { if (!response.ok) { throw new Error(`HTTP error ${response.status}`); } return response.json(); })
        .then(newAlerts => { errorCount = 0; if (newAlerts?.length > 0) { console.log(`Received ${newAlerts.length} new alert(s).`); lastKnownTimestamp = newAlerts[0].timestamp; if(latestTimestampSpan) latestTimestampSpan.dataset.timestamp = lastKnownTimestamp; playNotificationSound(); showNotification(newAlerts[0]); newAlertsReceived = true; updateTitleIndicator(); } else { console.debug("No new alerts."); } })
        .catch(error => { errorCount++; console.error(`Polling error (${errorCount}/${maxErrors}):`, error); if (errorCount >= maxErrors) { console.warn("Max polling errors. Stopping."); clearTimeout(pollTimeoutId); return; } })
        .finally(() => { if (errorCount < maxErrors && notificationPermission !== 'denied') { pollTimeoutId = setTimeout(pollForNewAlerts, pollingInterval); } });
    }
    if (enableNotificationsBtn) { if (notificationPermission === 'default') { enableNotificationsBtn.style.display = 'inline-block'; enableNotificationsBtn.onclick = requestNotificationPermission; } else { enableNotificationsBtn.style.display = 'none'; } }
    pollTimeoutId = setTimeout(pollForNewAlerts, 3000);

    // --- Acknowledge Button Logic ---
    const alertsTableBody = document.getElementById('alerts-table-body');
    if (alertsTableBody) {
        alertsTableBody.addEventListener('click', function(event) {
            const button = event.target.closest('.ack-button');
            if (button) {
                const alertId = button.dataset.alertId; if (!alertId) return;
                const comment = prompt("Optional comment for acknowledgment:", "");
                if (comment === null) return;

                console.log(`Ack ID: ${alertId}, Comment: "${comment}" (User: System)`);
                button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Ack...';

                fetch(`/api/alert/${alertId}/acknowledge`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ comment: comment }) })
                .then(response => { if (!response.ok) { return response.json().then(err => { throw new Error(err.message || `Ack failed: ${response.status}`); }).catch(() => { throw new Error(`Ack failed: ${response.status}`); }); } return response.json(); })
                .then(data => {
                    if (data.status === 'success') {
                        const ackContainer = button.closest('.ack-container'); const alertRow = button.closest('tr');
                        if (ackContainer && alertRow) {
                            const ts = data.acknowledged_at || new Date().toISOString();
                            const acknowledgedBy = data.acknowledged_by || "System";
                            const cmt = data.acknowledge_comment || "";
                            let tip = `Acknowledged by ${acknowledgedBy} at ${ts.replace('T',' ').split('.')[0]}`; if (cmt) { tip += ` | Comment: ${cmt.length > 50 ? cmt.substring(0,50)+'...' : cmt}`; }
                            ackContainer.innerHTML = `<span class="ack-indicator text-success w-100" data-bs-toggle="tooltip" data-bs-placement="left" title="${tip}"><i class="fas fa-user-check"></i> Seen</span>`;
                            initializeTooltips();
                            alertRow.classList.remove('status-firing'); alertRow.classList.add('status-acknowledged');
                            const msgCol = alertRow.querySelector('.message-col');
                            if (msgCol) {
                                let ackCommentDiv = msgCol.querySelector('.alert-ack-comment');
                                if (!ackCommentDiv) { ackCommentDiv = document.createElement('div'); ackCommentDiv.className = 'mt-2 pt-1 border-top border-dashed alert-ack-comment'; msgCol.appendChild(ackCommentDiv); }
                                if (cmt) { ackCommentDiv.innerHTML = `<span class="text-muted small fw-bold"><i class="fas fa-comment-dots me-1"></i> Ack Comment:</span><div class="ms-2 fst-italic">"${cmt}" <span class="text-muted small ms-1">- ${acknowledgedBy}</span></div>`; ackCommentDiv.style.display = 'block'; }
                                else { ackCommentDiv.style.display = 'none'; ackCommentDiv.innerHTML = ''; }
                            }
                         }
                         clearNewAlertsIndicator();
                    } else { button.disabled = false; button.innerHTML = '<i class="fas fa-check"></i> Ack'; alert(data.message); }
                })
                .catch(error => { console.error('Ack fetch error:', error); button.disabled = false; button.innerHTML = '<i class="fas fa-check"></i> Ack'; alert(`Error: ${error.message}`); });
            }
        });
    }

    // --- Collapse Icon Logic ---
    const detailsCollapseElements = document.querySelectorAll('.collapse[id^="details-"]');
    detailsCollapseElements.forEach(collapseEl => {
        const triggerLink = document.querySelector(`a[href="#${collapseEl.id}"]`); if (triggerLink) { const icon = triggerLink.querySelector('i.fas'); if (icon) { collapseEl.addEventListener('show.bs.collapse', () => { icon.classList.remove('fa-chevron-right'); icon.classList.add('fa-chevron-down'); triggerLink.setAttribute('aria-expanded', 'true'); }); collapseEl.addEventListener('hide.bs.collapse', () => { icon.classList.remove('fa-chevron-down'); icon.classList.add('fa-chevron-right'); triggerLink.setAttribute('aria-expanded', 'false'); }); } }
    });

}); // End DOMContentLoaded