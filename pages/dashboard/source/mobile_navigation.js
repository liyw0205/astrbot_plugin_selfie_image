    (function () {
      const menu = document.getElementById('bentoMobileMenu');
      const logoMenu = document.getElementById('sidebarLogoMenu');
      const shell = document.querySelector('header.app-shell');
      const backdrop = document.getElementById('sidebarBackdrop');
      if (!menu || !logoMenu || !shell) return;
      const mobile = window.matchMedia('(max-width: 1180px)');
      let pressTimer = null, pressedButton = null, pressStart = null;
      const clearNavPress = function () {
        clearTimeout(pressTimer);
        pressedButton?.classList.remove('nav-expanded');
        pressedButton = null;
        pressStart = null;
      };
      const syncMenu = function () {
        const open = shell.classList.contains('mobile-open');
        const rect = menu.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.top <= window.innerHeight;
        logoMenu.hidden = !mobile.matches || !document.body.classList.contains('authed') || visible || open;
        menu.setAttribute('aria-expanded', String(open));
        logoMenu.setAttribute('aria-expanded', String(open));
      };
      const setSidebarOpen = function (open) {
        clearNavPress();
        shell.classList.toggle('mobile-open', open);
        document.body.classList.toggle('sidebar-open', open);
        if (backdrop) backdrop.setAttribute('aria-hidden', open ? 'false' : 'true');
        syncMenu();
      };
      const closeSidebar = function () { setSidebarOpen(false); };
      [menu, logoMenu].forEach(function (button) {
        button.addEventListener('click', function () {
          setSidebarOpen(!shell.classList.contains('mobile-open'));
          if (shell.classList.contains('mobile-open')) {
            shell.querySelector('.page-nav button.active')?.focus({preventScroll: true});
          }
        });
      });
      if (backdrop) backdrop.addEventListener('click', function () {
        closeSidebar();
        (logoMenu.hidden ? menu : logoMenu).focus({preventScroll: true});
      });
      document.querySelectorAll('.page-nav button').forEach(function (button) {
        button.addEventListener('click', closeSidebar);
        button.addEventListener('pointerdown', function (event) {
          if (!event.isPrimary || event.button !== 0) return;
          clearNavPress();
          pressedButton = button;
          pressStart = {x: event.clientX, y: event.clientY};
          pressTimer = setTimeout(function () {
            button.classList.remove('nav-switching');
            button.classList.add('nav-expanded');
          }, 180);
        });
        button.addEventListener('pointermove', function (event) {
          if (pressStart && Math.hypot(event.clientX - pressStart.x, event.clientY - pressStart.y) > 10) clearNavPress();
        });
        button.addEventListener('pointerleave', clearNavPress);
        button.addEventListener('keydown', function (event) {
          if (event.key !== ' ' && event.key !== 'Enter') return;
          button.classList.remove('nav-switching');
          button.classList.add('nav-expanded');
        });
        button.addEventListener('keyup', function () { button.classList.remove('nav-expanded'); });
        button.addEventListener('blur', function () { button.classList.remove('nav-expanded'); });
        button.addEventListener('contextmenu', function (event) { event.preventDefault(); });
        button.addEventListener('animationend', function (event) {
          if (event.animationName === 'sidebar-select') button.classList.remove('nav-switching');
        });
      });
      window.addEventListener('pointerup', clearNavPress);
      window.addEventListener('pointercancel', clearNavPress);
      window.addEventListener('blur', clearNavPress);
      shell.addEventListener('scroll', clearNavPress, {passive: true});
      document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && shell.classList.contains('mobile-open') && !document.querySelector('.modal-mask.show')) {
          closeSidebar();
          (logoMenu.hidden ? menu : logoMenu).focus({preventScroll: true});
        }
      });
      mobile.addEventListener('change', function () {
        if (!mobile.matches) closeSidebar();
        else syncMenu();
      });
      if (typeof window.IntersectionObserver === 'function') {
        new IntersectionObserver(syncMenu).observe(menu);
      } else {
        window.addEventListener('scroll', syncMenu, {passive: true});
      }
      new MutationObserver(syncMenu).observe(document.body, {attributes: true, attributeFilter: ['class']});
      syncMenu();
    })();
