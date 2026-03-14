document.addEventListener('DOMContentLoaded', function () {
    const themeToggle = document.getElementById('theme-toggle');
    const root = document.documentElement;

    function applyTheme(theme) {
        if (theme === 'dark') {
            root.classList.add('dark-mode');
            if(themeToggle) themeToggle.checked = true;
        } else {
            root.classList.remove('dark-mode');
            if(themeToggle) themeToggle.checked = false;
        }
    }

    const currentTheme = localStorage.getItem('theme') || 'light';
    applyTheme(currentTheme);

    if(themeToggle) {
        themeToggle.addEventListener('change', function () {
            if (this.checked) {
                localStorage.setItem('theme', 'dark');
                applyTheme('dark');
            } else {
                localStorage.setItem('theme', 'light');
                applyTheme('light');
            }
        });
    }
});
