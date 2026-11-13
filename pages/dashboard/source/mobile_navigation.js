    (function () {
      const menu = document.getElementById('bentoMobileMenu');
      const shell = document.querySelector('header.app-shell');
      const backdrop = document.getElementById('sidebarBackdrop');
      const closeSidebar = function () {
        if (!shell) return;
        shell.classList.remove('mobile-open');
        document.body.classList.remove('sidebar-open');
        if (backdrop) backdrop.setAttribute('aria-hidden', 'true');
      };
      if (menu && shell) {
        menu.addEventListener('click', function () {
          const open = !shell.classList.contains('mobile-open');
          shell.classList.toggle('mobile-open', open);
          document.body.classList.toggle('sidebar-open', open);
          if (backdrop) backdrop.setAttribute('aria-hidden', open ? 'false' : 'true');
        });
      }
      if (backdrop) backdrop.addEventListener('click', closeSidebar);
      document.querySelectorAll('.page-nav button').forEach(function (button) {
        button.addEventListener('click', function () {
          closeSidebar();
        });
      });
    })();
