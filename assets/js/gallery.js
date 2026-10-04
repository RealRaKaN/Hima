(() => {
    const items = window.HimaGallery || [];
    const grid = document.getElementById('gallery-grid');
    const empty = document.getElementById('gallery-empty');
    const lb = document.getElementById('lightbox');
    const lbImg = document.getElementById('lb-img');
    const lbCap = document.getElementById('lb-caption');
    if (!grid || !lb) return;

    const lang = () => document.documentElement.lang === 'ar' ? 'ar' : 'en';
    const caption = (it) => it[lang()] || it.en || it.ar || '';
    let index = 0;

    const render = () => {
        grid.innerHTML = '';
        empty.hidden = items.length > 0;
        items.forEach((it, i) => {
            const fig = document.createElement('button');
            fig.type = 'button';
            fig.className = 'gallery-item glass fade-in visible';
            fig.innerHTML = `<img loading="lazy" src="${it.src}" alt="${caption(it)}">` +
                (caption(it) ? `<span class="gallery-caption">${caption(it)}</span>` : '');
            fig.addEventListener('click', () => open(i));
            grid.appendChild(fig);
        });
    };

    const show = () => {
        const it = items[index];
        lbImg.src = it.src;
        lbImg.alt = caption(it);
        lbCap.textContent = caption(it);
    };
    const open = (i) => {
        index = i;
        show();
        lb.hidden = false;
        document.body.style.overflow = 'hidden';
    };
    const close = () => {
        lb.hidden = true;
        document.body.style.overflow = '';
    };
    const step = (d) => {
        index = (index + d + items.length) % items.length;
        show();
    };

    document.getElementById('lb-close').addEventListener('click', close);
    document.getElementById('lb-prev').addEventListener('click', () => step(-1));
    document.getElementById('lb-next').addEventListener('click', () => step(1));
    lb.addEventListener('click', (e) => { if (e.target === lb) close(); });
    document.addEventListener('keydown', (e) => {
        if (lb.hidden) return;
        if (e.key === 'Escape') close();
        if (e.key === 'ArrowLeft') step(-1);
        if (e.key === 'ArrowRight') step(1);
    });

    // re-render captions when language changes
    new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
    render();
})();
