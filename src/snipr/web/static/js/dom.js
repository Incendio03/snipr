// DOM helpers. Everything that reaches the page from the API goes through
// h() as a text node, so untrusted strings are never parsed as HTML.

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Create an element. Strings and numbers among the children become text nodes.
 * Props: class, dataset, style (object), on<event> handlers, value, checked,
 * and plain attributes. null / undefined / false props are skipped, except
 * aria-* where false is written out as "false".
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  let value;
  if (props) {
    for (const [key, val] of Object.entries(props)) {
      if (key === 'value') { value = val; continue; }
      if (key.startsWith('aria-')) {
        if (val != null) el.setAttribute(key, String(val));
        continue;
      }
      if (val == null || val === false) continue;
      if (key === 'class') el.className = val;
      else if (key === 'dataset') Object.assign(el.dataset, val);
      else if (key === 'style') {
        for (const [prop, v] of Object.entries(val)) {
          if (prop.startsWith('--')) el.style.setProperty(prop, v);
          else el.style[prop] = v;
        }
      } else if (key.startsWith('on') && typeof val === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), val);
      } else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'required' || key === 'open') {
        el[key] = Boolean(val);
      } else if (val === true) el.setAttribute(key, '');
      else el.setAttribute(key, String(val));
    }
  }
  append(el, children);
  // Set after the children exist, so a <select> can find its option.
  if (value != null) el.value = value;
  return el;
}

export function append(el, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false || child === true) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Replace all children of el. */
export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

let uidCounter = 0;
export function uid(prefix = 'id') {
  uidCounter += 1;
  return `${prefix}-${uidCounter}`;
}

// Static icon paths (24 x 24 viewBox), taken from the mock where it has them.
const ICONS = {
  warn: { paths: ['M12 3 2 21h20L12 3z', 'M12 10v5', 'M12 18h.01'], width: 2 },
  back: { paths: ['M19 12H5', 'm11 6-6 6 6 6'], width: 2 },
  plus: { paths: ['M12 5v14', 'M5 12h14'], width: 2.4 },
  close: { paths: ['M6 6l12 12', 'M18 6 6 18'], width: 2 },
  check: { paths: ['m5 12.5 4.5 4.5L19 7.5'], width: 2.2 },
  play: { paths: ['M7 4v16l13-8z'], fill: true },
  pause: { paths: ['M6 4h4v16H6z', 'M14 4h4v16h-4z'], fill: true },
};

export function icon(name, size = 14) {
  const def = ICONS[name];
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('viewBox', '0 0 24 24');
  if (def.fill) {
    svg.setAttribute('fill', 'currentColor');
  } else {
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', String(def.width));
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
  }
  for (const d of def.paths) {
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

/** True when the event target is somewhere the user types text. */
export function isTypingTarget(target) {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target.tagName === 'TEXTAREA' || target.tagName === 'SELECT') return true;
  if (target.tagName === 'INPUT') {
    return !['checkbox', 'radio', 'range', 'button', 'submit'].includes(target.type);
  }
  return false;
}

/**
 * Re-render a region without dropping keyboard focus: the focused element is
 * found again afterwards by its data-key attribute.
 */
export function keepFocus(container, render) {
  const active = document.activeElement;
  const key = active && container.contains(active) ? active.getAttribute('data-key') : null;
  render();
  if (key) {
    for (const el of container.querySelectorAll('[data-key]')) {
      if (el.getAttribute('data-key') === key) { el.focus({ preventScroll: true }); break; }
    }
  }
}
