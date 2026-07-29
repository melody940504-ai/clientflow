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

  const showcase = document.querySelector('[data-product-showcase]');
  if (showcase) {
    const scenes = Array.from(showcase.querySelectorAll('[data-showcase-scene]'));
    const tabs = Array.from(showcase.querySelectorAll('[data-showcase-tab]'));
    const switcher = showcase.querySelector('.showcase-switcher');
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let activeIndex = 0;
    let rotationTimer = null;

    const showScene = (nextIndex, userInitiated = false) => {
      activeIndex = (nextIndex + scenes.length) % scenes.length;

      scenes.forEach((scene, index) => {
        const isActive = index === activeIndex;
        scene.classList.toggle('is-active', isActive);
        scene.setAttribute('aria-hidden', String(!isActive));
      });

      tabs.forEach((tab, index) => {
        const isActive = index === activeIndex;
        tab.classList.toggle('is-active', isActive);
        tab.setAttribute('aria-selected', String(isActive));
        tab.tabIndex = isActive ? 0 : -1;
      });

      switcher?.style.setProperty('--showcase-index', String(activeIndex));

      if (userInitiated && window.innerWidth <= 640) {
        tabs[activeIndex]?.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest', inline: 'center' });
      }
    };

    const stopRotation = () => {
      if (rotationTimer) window.clearInterval(rotationTimer);
      rotationTimer = null;
    };

    const startRotation = () => {
      stopRotation();
      if (!reduceMotion && document.visibilityState === 'visible') {
        rotationTimer = window.setInterval(() => showScene(activeIndex + 1), 5200);
      }
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => {
        showScene(index, true);
        startRotation();
      });

      tab.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const nextIndex = event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? tabs.length - 1
            : activeIndex + (event.key === 'ArrowRight' ? 1 : -1);
        showScene(nextIndex, true);
        tabs[activeIndex]?.focus();
        startRotation();
      });
    });

    showcase.addEventListener('mouseenter', stopRotation);
    showcase.addEventListener('mouseleave', startRotation);
    showcase.addEventListener('focusin', stopRotation);
    showcase.addEventListener('focusout', startRotation);
    document.addEventListener('visibilitychange', startRotation);

    showScene(0);
    startRotation();
  }
})();
