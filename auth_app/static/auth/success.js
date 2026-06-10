fetch('/auth/status', { credentials: 'include' })
  .then(r => r.json())
  .then(data => {
    if (!data.authenticated) return;
    document.getElementById('publisherIdDisplay').textContent = data.publisherId || '—';
    if (data.authenticatedAt) {
      document.getElementById('connectedAt').textContent =
        new Date(data.authenticatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
  })
  .catch(() => {});

document.getElementById('logoutBtn').addEventListener('click', async () => {
  await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
  window.location.href = '/connect';
});
