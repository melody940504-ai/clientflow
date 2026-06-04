(function () {
  const root = document.documentElement;
  const stored = localStorage.getItem('clientflow-theme');
  if (stored) root.setAttribute('data-theme', stored);
  const button = document.getElementById('themeToggle');
  if (button) {
    const updateLabel = () => {
      button.textContent = root.getAttribute('data-theme') === 'dark' ? 'Light mode' : 'Dark mode';
    };
    updateLabel();
    button.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      localStorage.setItem('clientflow-theme', next);
      updateLabel();
    });
  }
})();
