(function () {
  const root = document.documentElement;
  const themeKey = 'theme';
  const legacyThemeKey = 'clientflow-theme';
  const stored = localStorage.getItem(themeKey) || localStorage.getItem(legacyThemeKey);

  if (stored) root.setAttribute('data-theme', stored);

  const saveTheme = (theme) => {
    root.setAttribute('data-theme', theme);
    localStorage.setItem(themeKey, theme);
    localStorage.setItem(legacyThemeKey, theme);
  };

  const themeButtons = Array.from(document.querySelectorAll('[data-theme-toggle], #themeToggle'));
  const updateThemeLabels = () => {
    const isDark = root.getAttribute('data-theme') === 'dark';
    themeButtons.forEach((button) => {
      button.textContent = isDark ? 'Light mode' : 'Dark mode';
    });
  };

  themeButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      saveTheme(next);
      updateThemeLabels();
    });
  });
  updateThemeLabels();

  const menuToggle = document.getElementById('site-menu-toggle');
  const menuPanel = document.getElementById('site-menu-panel');

  if (menuToggle && menuPanel) {
    menuToggle.addEventListener('click', (event) => {
      event.stopPropagation();
      const isHidden = menuPanel.classList.toggle('hidden');
      menuToggle.setAttribute('aria-expanded', String(!isHidden));
    });

    menuPanel.addEventListener('click', (event) => {
      event.stopPropagation();
    });

    document.addEventListener('click', () => {
      menuPanel.classList.add('hidden');
      menuToggle.setAttribute('aria-expanded', 'false');
    });
  }
})();
