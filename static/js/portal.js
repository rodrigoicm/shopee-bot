function blockTermFromQueueBtn(btn) {
    const offerId = btn.getAttribute('data-offer-id') || '0';
    const suggested = btn.getAttribute('data-suggested') || '';
    const fullTitle = btn.getAttribute('data-title') || '';
    const retTab = btn.getAttribute('data-return-tab') || 'fila';
    const retLab = btn.getAttribute('data-return-lab') || 'shopee';
    const term = prompt(
        '🚫 Bloquear termo indesejado e remover da fila/laboratório:\n\nProduto: ' +
            fullTitle +
            '\n\nDigite a palavra ou termo que deseja banir (ex: almofada):',
        suggested
    );
    if (term && term.trim().length >= 2) {
        document.getElementById('quickBlockTermInput').value = term.trim();
        document.getElementById('quickBlockOfferIdInput').value = offerId;
        document.getElementById('quickBlockReturnTab').value = retTab;
        document.getElementById('quickBlockReturnLab').value = retLab;
        document.getElementById('quickBlockForm').submit();
    }
}

function toggleMlParent(parentCb) {
    const idx = parentCb.getAttribute('data-cat-idx');
    const subs = document.querySelectorAll('.ml-sub-check-' + idx);
    subs.forEach((cb) => {
        cb.checked = parentCb.checked;
    });
    parentCb.indeterminate = false;
    const badge = document.getElementById('mlSubCount' + idx);
    if (badge) {
        badge.textContent = (parentCb.checked ? subs.length : 0) + '/' + subs.length;
    }
}

function syncMlParentFromChild(childCb) {
    const idx = childCb.getAttribute('data-parent-idx');
    const parentCb = document.getElementById('mlCatCheck' + idx);
    const subs = document.querySelectorAll('.ml-sub-check-' + idx);
    let checkedCount = 0;
    subs.forEach((cb) => {
        if (cb.checked) checkedCount++;
    });
    if (parentCb) {
        parentCb.checked = checkedCount > 0;
        parentCb.indeterminate = checkedCount > 0 && checkedCount < subs.length;
    }
    const badge = document.getElementById('mlSubCount' + idx);
    if (badge) {
        badge.textContent = checkedCount + '/' + subs.length;
    }
}

async function validateShopeeVideo() {
    const urlInput = document.getElementById('productUrlInput');
    const manualInput = document.getElementById('manualVideoInput');
    const box = document.getElementById('videoValidationBox');
    const statusEl = document.getElementById('videoValidationStatus');
    const player = document.getElementById('videoPreviewPlayer');
    const check = document.getElementById('extractVideoCheck');
    const prodVal = urlInput.value.trim();
    const manualVal = manualInput.value.trim();

    if (!prodVal && !manualVal) {
        alert('Cole o link do produto ou o link direto do vídeo (.mp4) primeiro!');
        return;
    }
    if (prodVal.toLowerCase().includes('.mp4') && !manualVal) {
        manualInput.value = prodVal;
        urlInput.value = '';
    }

    box.style.display = 'block';
    player.style.display = 'none';
    statusEl.innerHTML = '⏳ Validando vídeo... aguarde.';

    const formData = new FormData();
    formData.append('product_url', urlInput.value.trim());
    formData.append('manual_video_url', manualInput.value.trim());

    try {
        const resp = await fetch('/validate-video', { method: 'POST', body: formData });
        const data = await resp.json();
        statusEl.innerHTML = data.message;
        if (data.success && data.video_url) {
            player.src = data.video_url;
            player.style.display = 'block';
            manualInput.value = data.video_url;
            check.checked = true;
        }
    } catch (err) {
        statusEl.innerHTML = '❌ Erro na requisição: ' + err;
    }
}

function restaurarMixPadrao() {
    const shopee = document.querySelector('input[name="mix_shopee_pct"]');
    const amazon = document.querySelector('input[name="mix_amazon_pct"]');
    const amazonCheck = document.querySelector('input[name="amazon_auto_enabled"]');
    const mlCheck = document.querySelector('input[name="ml_auto_enabled"]');
    if (shopee) shopee.value = 40;
    if (amazon) amazon.value = 40;
    if (amazonCheck) amazonCheck.checked = true;
    if (mlCheck) mlCheck.checked = true;
}

document.addEventListener('DOMContentLoaded', () => {
    const timerEl = document.getElementById('countdown-timer');
    if (!timerEl) return;
    let secondsLeft = parseInt(timerEl.dataset.seconds || '0', 10);

    function updateCountdown() {
        if (secondsLeft <= 0) {
            timerEl.textContent = '00:00';
            return;
        }
        const mins = Math.floor(secondsLeft / 60);
        const secs = secondsLeft % 60;
        timerEl.textContent =
            String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
        secondsLeft--;
        if (secondsLeft === 0) {
            setTimeout(() => window.location.reload(), 1000);
        }
    }

    if (secondsLeft > 0) {
        updateCountdown();
        setInterval(updateCountdown, 1000);
    }
});
