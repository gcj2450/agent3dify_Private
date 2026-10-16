const paths = {
  cube: '<path d="m12 3 8.5 4.8v8.4L12 21l-8.5-4.8V7.8L12 3Z"/><path d="m3.5 7.8 8.5 4.8 8.5-4.8M12 12.6V21M12 3v9.6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  settings: '<path d="m9.5 3-.8 2.4-2.3 1-2.3-.5-1.5 2.6 1.5 1.8v2.8l-1.5 1.8 1.5 2.6 2.3-.5 2.3 1 .8 2.4h3l.8-2.4 2.3-1 2.3.5 1.5-2.6-1.5-1.8v-2.8l1.5-1.8-1.5-2.6-2.3.5-2.3-1-.8-2.4h-3Z"/><circle cx="11" cy="12" r="3"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
  arrowRight: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  arrowUp: '<path d="M12 20V4m-6 6 6-6 6 6"/>',
  paperclip: '<path d="m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7L12 3a2 2 0 0 1 3 3l-9 9a1 1 0 0 0 1.5 1.5L16 8"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  terminal: '<path d="m5 6 5 5-5 5m8 1h6"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-5 4 4 4-7 4 5"/>',
  code: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  wireframe: '<path d="m12 3 9 5v8l-9 5-9-5V8l9-5Zm0 0v18M3 8l18 8M21 8 3 16"/>',
  grid: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18m6-18v18M3 9h18M3 15h18"/>',
  expand: '<path d="M9 3H3v6m12-6h6v6M3 15v6h6m12-6v6h-6"/>',
  folder: '<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  file: '<path d="M14 2H5v20h14V7l-5-5Zm0 0v5h5M8 12h8m-8 4h6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  chat: '<path d="M21 15a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v9Z"/>',
};
export function icon(name, size = 18) { return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.file}</svg>`; }
export function hydrateIcons(root = document) { root.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); }); }
