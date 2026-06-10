document.getElementById('toggleSecret').addEventListener('click', function () {
  const input = document.getElementById('apiSecret');
  input.type = input.type === 'password' ? 'text' : 'password';
});

['publisherId', 'apiKey', 'apiSecret'].forEach(id => {
  document.getElementById(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') handleConnect();
  });
});

document.getElementById('connectBtn').addEventListener('click', handleConnect);

async function handleConnect() {
  const publisherId = document.getElementById('publisherId').value.trim();
  const apiKey      = document.getElementById('apiKey').value.trim();
  const apiSecret   = document.getElementById('apiSecret').value.trim();
  const btn         = document.getElementById('connectBtn');
  const spinner     = document.getElementById('spinner');
  const btnText     = document.getElementById('btnText');

  hideError();

  if (!publisherId || !apiKey || !apiSecret) {
    showError('Please fill in all fields.');
    return;
  }

  btn.disabled = true;
  spinner.style.display = 'block';
  btnText.textContent = 'Verifying credentials…';

  try {
    const res  = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ publisherId, apiKey, apiSecret }),
      credentials: 'include',
    });
    const data = await res.json();

    if (data.success) {
      spinner.style.display = 'none';
      btnText.textContent = '✓ Connected!';
      setTimeout(() => { window.location.href = data.redirectTo; }, 400);
    } else {
      showError(data.error || 'Authentication failed.');
      resetBtn();
    }
  } catch {
    showError('Network error. Is the server running?');
    resetBtn();
  }
}

function showError(msg) {
  document.getElementById('errorText').textContent = msg;
  document.getElementById('errorBox').classList.remove('hidden');
}

function hideError() {
  document.getElementById('errorBox').classList.add('hidden');
}

function resetBtn() {
  document.getElementById('connectBtn').disabled = false;
  document.getElementById('spinner').style.display = 'none';
  document.getElementById('btnText').textContent = 'Connect to Claude';
}
