document.getElementById('toggleSecret').addEventListener('click', function () {
  const input   = document.getElementById('apiSecret');
  const isHidden = input.type === 'password';
  input.type = isHidden ? 'text' : 'password';
  this.querySelector('svg').style.opacity = isHidden ? '0.5' : '1';
});

document.getElementById('authorizeForm').addEventListener('submit', function () {
  const btn     = document.getElementById('authorizeBtn');
  const spinner = document.getElementById('spinner');
  const btnText = document.getElementById('btnText');
  btn.disabled = true;
  spinner.style.display = 'block';
  btnText.textContent = 'Verifying credentials…';
});
