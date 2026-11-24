// The in-browser half of loupe's rendered scan. render.py runs this in a
// JavaScript world of its own on the page's main frame, and on every frame
// it reaches: page scripts share the DOM with it but not its globals or
// prototypes, so a page can't patch checkVisibility or getClientRects to make
// hidden text look visible.
//
// It installs one object, loupeScanner. render.py calls watchStatic() as soon
// as the page's HTML has arrived, recordLoad() once the page has loaded,
// recheck() after every scroll step and every centreNext(), and finish() at
// the end.
(() => {
  "use strict";

  // Text inside these is not page copy (scripts, styles), is fallback content
  // a browser shows only when it can't play or draw the element, or is drawn
  // by the browser as part of a form control. The static scan covers noscript.
  const EXCLUDED = [
    "script", "style", "template", "noscript", "head", "select", "datalist", "textarea",
    "canvas", "video", "audio", "object", "iframe", "embed", "picture", "rp",
    "svg title", "svg desc", "svg metadata", "svg style", "svg script",
  ].join(", ");

  // Screen-reader-only styles shrink text to a 1px box. A single pixel shows
  // nothing readable, so anything thinner than this counts as unseen.
  const MIN_READABLE_PX = 2;

  // Text faded almost to nothing is as unreadable as text at opacity 0, and
  // opacity multiplies down the tree: 0.2 inside 0.2 shows at 0.04.
  const MIN_VISIBLE_OPACITY = 0.05;

  // Text whose contrast with the colour behind it is under this ratio is
  // too faint to see. WCAG 2.2 asks for 4.5:1 for body text and 3:1 for large
  // text; text between this and those is hard to read, an accessibility
  // problem, not hiding. #ccc on white is 1.6:1, faint but readable, and
  // #ddd on white 1.36:1. docs/how-it-works.md explains the choice.
  const MIN_CONTRAST = 1.5;
  const CONTRAST_REASON = "coloured like its background";
  const WHITE = [255, 255, 255, 1];

  const VISIBILITY_OPTIONS = { opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true };
  // A display:contents element has no box of its own, so its nearest boxed
  // ancestor is checked instead, and the element's own visibility separately.
  const BOX_OPTIONS = { opacityProperty: true, contentVisibilityAuto: true };
  const NO_OVERFLOW_DISPLAYS = new Set(["inline", "contents", "none"]);

  // The ways scroll animations hide content until it is in view. Only text
  // hidden in these ways alone is worth centring; display:none,
  // visibility:hidden and closed <details> wait for a click, not a position.
  const CENTRABLE_REASONS = new Set([
    "opacity:0",
    "near-zero opacity",
    "outside the page",
    "clipped",
    "too small to see",
    "content-visibility:auto",
  ]);

  // Element -> { atLoad: true | false | null, later: boolean }. atLoad is null
  // for elements that weren't in the page yet when it finished loading.
  const seen = new Map();
  let scrollTarget = null;
  // Elements centreNext() has already tried, or found to have no box.
  const centred = new Set();
  // The static candidates passed to watchStatic(): "tag|text" keys (text with
  // whitespace removed) to candidate indexes, and each candidate's own key.
  let staticWanted = new Map();
  let staticKeys = [];
  let staticTags = new Set();
  let staticCount = 0;
  let longestStaticText = 0;
  // For each candidate, the text elements of every live element with the
  // same tag and text, as matched at load by matchStatic().
  let staticCopies = [];

  // A carousel can show a candidate's text and take it away again before
  // the first check or between two checks, which two snapshots would call
  // hidden. So from watchStatic() until finish(), the live copies of every
  // candidate are tracked as they appear, and checked every
  // WATCH_INTERVAL_MS and whenever nodes are added.
  const WATCH_INTERVAL_MS = 250;
  // For each candidate, the live elements with its tag and text.
  let watchedCopies = [];
  // For each candidate, the page time any copy was first visible, or null.
  let firstVisibleAt = [];
  // The page time of recordLoad(), the first check.
  let firstCheckAt = null;
  let watching = false;
  let watchTimer = null;
  let watchObserver = null;
  let sampleQueued = false;
  let watchSamples = 0;
  let watchError = null;
  let hasPainted = false;
  // Visible text elements on an image or a gradient of several colours,
  // whose contrast with what's behind them can't be judged from styles.
  const unjudged = new Set();

  // Closed shadow roots, by host, that render.py found through the DevTools
  // protocol and passed in. Neither the page's scripts nor element.shadowRoot
  // can reach them, but the browser draws them like any other.
  const closedRoots = new Map();
  // Text elements with text of their own written straight into a shadow root.
  const shadowOwned = new Set();
  // Elements that hold a frame -> whether any check saw their box.
  const frameOwners = new Map();
  const FRAME_OWNER_TAGS = new Set(["iframe", "frame", "object", "embed"]);

  function shadowRootOf(element) {
    return element.shadowRoot ?? closedRoots.get(element) ?? null;
  }

  // The element a text node is the own text of: its parent, or the host of
  // the shadow root it's written straight into.
  function ownerOf(node) {
    const parent = node.parentNode;
    if (parent instanceof ShadowRoot) return parent.host;
    return parent instanceof Element ? parent : null;
  }

  // A node's parent in the tree the browser lays out and paints: the slot it
  // is assigned to, the host of the shadow root it's in, or its parent.
  function flatParent(node) {
    const slot = node.assignedSlot;
    if (slot !== null && slot !== undefined) return slot;
    const parent = node.parentNode;
    if (parent instanceof ShadowRoot) return parent.host;
    return parent instanceof Element ? parent : null;
  }

  // element.closest(selector), carried on out of each shadow root to its host.
  function composedClosest(element, selector) {
    for (let node = element; node !== null; ) {
      const match = node.closest(selector);
      if (match !== null) return match;
      const root = node.getRootNode();
      node = root instanceof ShadowRoot ? root.host : null;
    }
    return null;
  }

  // The text nodes that are element's own text, its shadow root's included.
  function ownTextNodes(element) {
    const nodes = [];
    const shadow = shadowRootOf(element);
    for (const parent of shadow === null ? [element] : [shadow, element]) {
      for (const child of parent.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) nodes.push(child);
      }
    }
    return nodes;
  }

  // Calls visit for root and every element and text node under it, going
  // into each shadow root right after its host, as a reader meets them.
  function forEachComposed(root, visit) {
    const show = NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT;
    const walkers = [document.createTreeWalker(root, show)];
    const enter = (node) => {
      visit(node);
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const shadow = shadowRootOf(node);
      if (shadow !== null) walkers.push(document.createTreeWalker(shadow, show));
    };
    if (root.nodeType === Node.ELEMENT_NODE) enter(root);
    while (walkers.length > 0) {
      const node = walkers[walkers.length - 1].nextNode();
      if (node === null) walkers.pop();
      else enter(node);
    }
  }

  // Every element with text of its own under root, shadow roots included,
  // and every element holding a frame, in the order a reader meets them.
  function readTree(root = document.body) {
    const elements = [];
    const owners = [];
    if (root === null) return { elements, owners };
    const accepted = new Set();
    const rejected = new Set();
    forEachComposed(root, (node) => {
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (FRAME_OWNER_TAGS.has(node.localName)) owners.push(node);
        return;
      }
      const owner = ownerOf(node);
      if (owner === null || rejected.has(owner) || node.data.trim() === "") return;
      if (!accepted.has(owner)) {
        if (composedClosest(owner, EXCLUDED) !== null) {
          rejected.add(owner);
          return;
        }
        accepted.add(owner);
        elements.push(owner);
      }
      if (node.parentNode instanceof ShadowRoot) shadowOwned.add(owner);
    });
    return { elements, owners };
  }

  function collectTextElements(root = document.body) {
    return readTree(root).elements;
  }

  // One pass caches computed styles and box geometry, which don't change
  // while a single check runs.
  function newPass() {
    const styles = new Map();
    const boxes = new Map();
    const style = (element) => {
      let value = styles.get(element);
      if (value === undefined) {
        value = getComputedStyle(element);
        styles.set(element, value);
      }
      return value;
    };
    const rootStyle = style(document.documentElement);
    const bodyOverflowGoesToViewport = rootStyle.overflowX === "visible" && rootStyle.overflowY === "visible";
    const box = (element) => {
      let value = boxes.get(element);
      if (value === undefined) {
        value = describeBox(element, style(element), bodyOverflowGoesToViewport && element === document.body);
        boxes.set(element, value);
      }
      return value;
    };
    // Fixed and sticky elements, dialogs and open popovers stay where they
    // are on screen while the page scrolls under them, and so does whatever
    // is inside one.
    const pinnedCache = new Map();
    const pinned = (element) => {
      let value = pinnedCache.get(element);
      if (value === undefined) {
        const position = style(element).position;
        const parent = flatParent(element);
        value =
          position === "fixed" ||
          position === "sticky" ||
          element.localName === "dialog" ||
          isOpenPopover(element) ||
          (parent !== null && pinned(parent));
        pinnedCache.set(element, value);
      }
      return value;
    };
    return { style, box, pinned, viewport: describeViewport(style) };
  }

  function isOpenPopover(element) {
    try {
      return element.matches(":popover-open");
    } catch (error) {
      // A browser without popovers has none open.
      return false;
    }
  }

  function createsFixedContainingBlock(s) {
    return (
      s.transform !== "none" ||
      s.translate !== "none" ||
      s.rotate !== "none" ||
      s.scale !== "none" ||
      s.perspective !== "none" ||
      s.filter !== "none" ||
      (s.backdropFilter !== undefined && s.backdropFilter !== "" && s.backdropFilter !== "none") ||
      /\b(paint|layout|strict|content)\b/.test(s.contain) ||
      /\b(transform|translate|rotate|scale|perspective|filter)\b/.test(s.willChange) ||
      (s.containerType !== undefined && s.containerType !== "" && s.containerType !== "normal")
    );
  }

  function describeBox(element, s, overflowGoesToViewport) {
    const appliesOverflow = !overflowGoesToViewport && !NO_OVERFLOW_DISPLAYS.has(s.display);
    const paintContained = /\b(paint|strict|content)\b/.test(s.contain);
    const axis = (value) => {
      if (!appliesOverflow) return "none";
      if (value === "hidden" || value === "clip") return "clip";
      if (value === "auto" || value === "scroll") return "scroll";
      return paintContained ? "clip" : "none";
    };
    const fixedContainer = createsFixedContainingBlock(s);
    const info = {
      position: s.position,
      containsAbsolute: s.position !== "static" || fixedContainer,
      containsFixed: fixedContainer,
      overflowX: axis(s.overflowX),
      overflowY: axis(s.overflowY),
      rtl: s.direction === "rtl",
      padding: null,
      originX: 0,
      originY: 0,
      clipRect: null,
      clipPathRect: null,
    };
    const hasClip = s.clip !== "" && s.clip !== "auto";
    const hasClipPath = s.clipPath !== "" && s.clipPath !== "none";
    if (info.overflowX !== "none" || info.overflowY !== "none" || hasClip || hasClipPath) {
      const border = element.getBoundingClientRect();
      const left = border.left + (parseFloat(s.borderLeftWidth) || 0);
      const top = border.top + (parseFloat(s.borderTopWidth) || 0);
      info.padding = { left, top, right: left + element.clientWidth, bottom: top + element.clientHeight };
      info.originX = left - element.scrollLeft;
      info.originY = top - element.scrollTop;
      info.clipRect = hasClip ? clipPropertyRect(s, border) : null;
      info.clipPathRect = hasClipPath ? clipPathRect(s.clipPath, border) : null;
    }
    return info;
  }

  // The legacy clip property, which applies only to absolutely positioned boxes.
  function clipPropertyRect(s, border) {
    if (s.position !== "absolute" && s.position !== "fixed") return null;
    const match = /^rect\(([^)]*)\)$/.exec(s.clip);
    if (match === null) return null;
    const parts = match[1].split(/[\s,]+/).filter((part) => part !== "");
    if (parts.length !== 4) return null;
    const value = (part, whenAuto) => (part === "auto" ? whenAuto : parseFloat(part));
    const top = value(parts[0], 0);
    const right = value(parts[1], border.width);
    const bottom = value(parts[2], border.height);
    const left = value(parts[3], 0);
    if ([top, right, bottom, left].some(Number.isNaN)) return null;
    return { left: border.left + left, top: border.top + top, right: border.left + right, bottom: border.top + bottom };
  }

  // inset() and zero-radius circles or ellipses, the shapes used to hide
  // text. Any other shape is treated as not clipping.
  function clipPathRect(value, border) {
    const inset = /^inset\(([^)]*)\)/.exec(value);
    if (inset !== null) {
      const parts = inset[1].split(/\s+round\s+/)[0].trim().split(/\s+/);
      if (parts[0] === "" || parts.length > 4) return null;
      const [top, right = top, bottom = top, left = right] = parts;
      const length = (part, size) => {
        if (part.endsWith("%")) return (parseFloat(part) / 100) * size;
        if (part.endsWith("px") || part === "0") return parseFloat(part);
        return NaN;
      };
      const t = length(top, border.height);
      const r = length(right, border.width);
      const b = length(bottom, border.height);
      const l = length(left, border.width);
      if ([t, r, b, l].some(Number.isNaN)) return null;
      return { left: border.left + l, top: border.top + t, right: border.right - r, bottom: border.bottom - b };
    }
    if (/^(circle|ellipse)\(\s*0(px|%)?[\s)]/.test(value)) {
      return { left: border.left, top: border.top, right: border.left, bottom: border.top };
    }
    return null;
  }

  // The part of the viewport's coordinate space a reader can scroll to.
  function describeViewport(style) {
    const root = document.documentElement;
    const rootStyle = style(root);
    let overflowX = rootStyle.overflowX;
    let overflowY = rootStyle.overflowY;
    if (overflowX === "visible" && overflowY === "visible" && document.body !== null) {
      const bodyStyle = style(document.body);
      overflowX = bodyStyle.overflowX;
      overflowY = bodyStyle.overflowY;
    }
    const scroller = document.scrollingElement || root;
    const width = scroller.clientWidth;
    const height = scroller.clientHeight;
    const locksX = overflowX === "hidden" || overflowX === "clip";
    const locksY = overflowY === "hidden" || overflowY === "clip";
    let reachX;
    if (locksX) reachX = [0, width];
    else if (rootStyle.direction === "rtl") reachX = [-Infinity, width - window.scrollX];
    else reachX = [-window.scrollX, Infinity];
    const reachY = locksY ? [0, height] : [-window.scrollY, Infinity];
    return { width, height, scrollsY: !locksY, reachX, reachY };
  }

  // Follows one rectangle of element's text up through everything that can
  // clip it, or with ownBox, element's own box, which its own overflow
  // doesn't clip. Returns why a reader can't see it, or null when enough of
  // it shows.
  function reach(element, rect, pass, ownBox = false) {
    let x0 = rect.left;
    let x1 = rect.right;
    let y0 = rect.top;
    let y1 = rect.bottom;
    const clipTo = (r) => {
      x0 = Math.max(x0, r.left);
      x1 = Math.min(x1, r.right);
      y0 = Math.max(y0, r.top);
      y1 = Math.min(y1, r.bottom);
    };
    const tooSmall = () => x1 - x0 < MIN_READABLE_PX || y1 - y0 < MIN_READABLE_PX;

    // Overflow clipping only reaches a box through its containing blocks: an
    // absolutely positioned box escapes overflow:hidden on unpositioned
    // ancestors, and a fixed one escapes everything but transformed ancestors.
    let flow = "static";
    let start = element;
    if (ownBox) {
      const own = pass.box(element);
      if (own.clipRect !== null) clipTo(own.clipRect);
      if (own.clipPathRect !== null) clipTo(own.clipPathRect);
      if (tooSmall()) return "clipped";
      flow = own.position === "absolute" || own.position === "fixed" ? own.position : "static";
      start = flatParent(element);
    }
    for (let node = start; node !== null && node !== document.documentElement; node = flatParent(node)) {
      const box = pass.box(node);
      const contains = flow === "fixed" ? box.containsFixed : flow === "absolute" ? box.containsAbsolute : true;
      if (contains) {
        if (box.overflowX === "clip") {
          x0 = Math.max(x0, box.padding.left);
          x1 = Math.min(x1, box.padding.right);
        } else if (box.overflowX === "scroll") {
          // Content before a scroll container's start edge can't be scrolled
          // to. Anything else can be brought into the container's box.
          if (!box.rtl && x1 <= box.originX) return "outside the page";
          x0 = box.padding.left;
          x1 = box.padding.right;
        }
        if (box.overflowY === "clip") {
          y0 = Math.max(y0, box.padding.top);
          y1 = Math.min(y1, box.padding.bottom);
        } else if (box.overflowY === "scroll") {
          if (y1 <= box.originY) return "outside the page";
          y0 = box.padding.top;
          y1 = box.padding.bottom;
        }
        flow = box.position === "absolute" || box.position === "fixed" ? box.position : "static";
      }
      if (box.clipRect !== null) clipTo(box.clipRect);
      if (box.clipPathRect !== null) clipTo(box.clipPathRect);
      if (tooSmall()) return "clipped";
    }

    const viewport = pass.viewport;
    if (flow === "fixed") {
      clipTo({ left: 0, top: 0, right: viewport.width, bottom: viewport.height });
    } else {
      const [left, right] = viewport.reachX;
      const [top, bottom] = viewport.reachY;
      clipTo({ left, right, top, bottom });
    }
    return tooSmall() ? "outside the page" : null;
  }

  // The boxes of a text node's text, without the white space around it,
  // that are big enough to read.
  function textBoxes(node) {
    const start = node.data.search(/\S/);
    if (start === -1) return [];
    const end = node.data.length - /\s*$/.exec(node.data)[0].length;
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, end);
    return Array.from(range.getClientRects()).filter(
      (rect) => rect.width >= MIN_READABLE_PX && rect.height >= MIN_READABLE_PX,
    );
  }

  function geometryReason(element, pass) {
    let reason = "too small to see";
    for (const child of ownTextNodes(element)) {
      for (const rect of textBoxes(child)) {
        const blocked = reach(element, rect, pass);
        if (blocked === null) return null;
        reason = blocked;
      }
    }
    return reason;
  }

  // hidden="until-found" works through content-visibility:hidden, so name the attribute when it's the cause.
  function contentVisibilityReason(node) {
    return node.getAttribute("hidden") === "until-found" ? "hidden until found" : "content-visibility:hidden";
  }

  function notRenderedReason(element, pass) {
    // Everything in a closed <details> is hidden but its first <summary>.
    let previous = null;
    for (let node = element; node !== null; previous = node, node = flatParent(node)) {
      if (node.localName !== "details" || node.open) continue;
      const summary = Array.from(node.children).find((child) => child.localName === "summary");
      if (summary === undefined || previous !== summary) return "inside a closed <details>";
    }
    for (let node = element; node !== null; node = flatParent(node)) {
      const s = pass.style(node);
      if (s.display === "none") return node.hasAttribute("hidden") ? "hidden attribute" : "display:none";
      if (node !== element && s.contentVisibility === "hidden") return contentVisibilityReason(node);
    }
    return "not rendered";
  }

  function styleReasons(box, pass, includeVisibility) {
    const reasons = [];
    const visibility = pass.style(box).visibility;
    if (includeVisibility && (visibility === "hidden" || visibility === "collapse")) reasons.push("visibility:hidden");
    for (let node = box; node !== null; node = flatParent(node)) {
      if (parseFloat(pass.style(node).opacity) === 0) {
        reasons.push("opacity:0");
        break;
      }
    }
    if (reasons.length === 0) reasons.push("content-visibility:auto");
    return reasons;
  }

  // The opacity a box is drawn at. display:contents elements have no box,
  // so their opacity doesn't apply.
  function combinedOpacity(box, pass) {
    let product = 1;
    for (let node = box; node !== null; node = flatParent(node)) {
      const s = pass.style(node);
      if (s.display === "contents") continue;
      const opacity = parseFloat(s.opacity);
      if (Number.isFinite(opacity)) product *= opacity;
      if (product < MIN_VISIBLE_OPACITY) break;
    }
    return product;
  }

  function nearestBoxedAncestor(element, pass) {
    for (let node = flatParent(element); node !== null; node = flatParent(node)) {
      if (pass.style(node).display !== "contents") return node;
    }
    return null;
  }

  // [r, g, b, a] from a computed colour, which browsers give as rgb() or
  // rgba() for sRGB colours. Anything else, such as oklch(), is null.
  function parseColor(value) {
    const match = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(value);
    if (match === null) return null;
    let alpha = 1;
    if (match[4] !== undefined) alpha = match[4].endsWith("%") ? parseFloat(match[4]) / 100 : parseFloat(match[4]);
    return [parseFloat(match[1]), parseFloat(match[2]), parseFloat(match[3]), alpha];
  }

  // top drawn over bottom, by alpha compositing.
  function blend(top, bottom) {
    const alpha = top[3] + bottom[3] * (1 - top[3]);
    if (alpha === 0) return [0, 0, 0, 0];
    const channel = (index) => (top[index] * top[3] + bottom[index] * bottom[3] * (1 - top[3])) / alpha;
    return [channel(0), channel(1), channel(2), alpha];
  }

  // WCAG's relative luminance.
  function luminance(color) {
    const linear = (value) => {
      const c = value / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * linear(color[0]) + 0.7152 * linear(color[1]) + 0.0722 * linear(color[2]);
  }

  // A background image that's a gradient with every stop the same colour
  // paints that one colour. Anything else, an image or a gradient of
  // several colours, is null: what's behind the text varies.
  function singleColourGradient(value) {
    if (/url\(/.test(value) || !/^(?:repeating-)?(?:linear|radial|conic)-gradient\(/.test(value)) return null;
    if (/\)\s*,\s*(?:repeating-)?(?:linear|radial|conic)-gradient\(/.test(value)) return null;
    const stops = value.match(/rgba?\([^)]*\)/g);
    if (stops === null) return null;
    const colors = stops.map(parseColor);
    if (colors.some((color) => color === null)) return null;
    const first = colors[0];
    return colors.every((color) => color.every((part, index) => part === first[index])) ? first : null;
  }

  // Elements that draw pictures, not a colour a style can say.
  const PICTURES = new Set(["IMG", "VIDEO", "CANVAS", "PICTURE", "IFRAME", "EMBED", "OBJECT", "svg"]);

  // Every element whose box covers the viewport point x, y, topmost first, as
  // seen from element's own tree, so an element in a shadow root is listed
  // itself rather than as its host. Elements with pointer-events:none aren't
  // listed: hit testing skips them.
  function stackAt(element, x, y) {
    const root = element.getRootNode();
    return (root instanceof ShadowRoot ? root : document).elementsFromPoint(x, y);
  }

  function onScreen(x, y) {
    return x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight;
  }

  // What's painted under the middle of element's first box, the element
  // itself first, when it's on screen; its ancestors otherwise.
  function paintedBelow(element) {
    const rect = element.getClientRects()[0];
    if (rect !== undefined && rect.width > 0 && rect.height > 0) {
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      if (onScreen(x, y)) {
        const stack = stackAt(element, x, y);
        const index = stack.indexOf(element);
        if (index >= 0) return stack.slice(index);
      }
    }
    const chain = [];
    for (let node = element; node !== null; node = flatParent(node)) chain.push(node);
    return chain;
  }

  // The colour behind element's text: the backgrounds painted under it,
  // composited down to the first opaque one, over white. null when a
  // picture, an image or a gradient of several colours is in the way.
  function backgroundBehind(element, pass) {
    const layers = [];
    for (const node of paintedBelow(element)) {
      if (node !== element && PICTURES.has(node.nodeName)) return null;
      const s = pass.style(node);
      if (s.backgroundImage !== "none") {
        const color = singleColourGradient(s.backgroundImage);
        if (color === null) return null;
        layers.push(color);
      }
      const background = parseColor(s.backgroundColor);
      if (background === null) return null;
      if (background[3] > 0) layers.push(background);
      if (layers.length > 0 && layers[layers.length - 1][3] >= 1) break;
    }
    return layers.reduceRight((below, layer) => blend(layer, below), WHITE);
  }

  // CONTRAST_REASON when element's own text is too close in colour to what's
  // behind it to see, null otherwise. Text it can't judge goes in unjudged.
  function contrastReason(element, pass) {
    const s = pass.style(element);
    // A shadow or an outline can make text stand out whatever its colour.
    if (s.textShadow !== "none" || parseFloat(s.webkitTextStrokeWidth) > 0) return null;
    const fill = parseColor(s.webkitTextFillColor || s.color);
    const behind = backgroundBehind(element, pass);
    if (fill === null || behind === null) {
      unjudged.add(element);
      return null;
    }
    unjudged.delete(element);
    const text = luminance(blend(fill, behind));
    const background = luminance(behind);
    const ratio = (Math.max(text, background) + 0.05) / (Math.min(text, background) + 0.05);
    return ratio < MIN_CONTRAST ? CONTRAST_REASON : null;
  }

  // A box painted over text hides it only where it paints a solid colour at
  // least this opaque.
  const MIN_COVER_ALPHA = 0.95;
  const COVERED_REASON = "covered by another element";
  // The points of each box of text that hit testing tries, as fractions of
  // the box's width and height, and how many of an element's boxes it tries.
  const COVER_POINTS = [
    [0.5, 0.5],
    [0.15, 0.3],
    [0.85, 0.3],
    [0.15, 0.7],
    [0.85, 0.7],
  ];
  const MAX_COVER_BOXES = 4;

  // What one check makes of a piece of text, or of a frame's box.
  const VISIBLE = "visible";
  // Visible every other way, but with no point on screen to hit-test.
  const OFF_SCREEN = "off screen";
  const COVERED = "covered";
  const HIDDEN = "hidden";
  // A point under a picture, whose transparency can't be told.
  const UNKNOWN = "unknown";

  // Visible text elements under an image, a picture or a gradient of
  // several colours, which may or may not let the text show through.
  const unjudgedCover = new Set();

  // "solid" when element paints a nearly opaque colour over what's below it,
  // "picture" when it paints an image, a picture or a gradient of several
  // colours, and null when it paints nothing that could hide text.
  function coverPaint(element, pass) {
    if (combinedOpacity(element, pass) < MIN_COVER_ALPHA) return null;
    if (PICTURES.has(element.nodeName)) return "picture";
    const s = pass.style(element);
    if (s.mixBlendMode !== "normal") return null;
    // A background clipped to the element's own text paints only its letters.
    if (s.backgroundClip === "text" || s.webkitBackgroundClip === "text") return null;
    if (s.backgroundImage !== "none") {
      const colour = singleColourGradient(s.backgroundImage);
      if (colour === null) return "picture";
      if (colour[3] >= MIN_COVER_ALPHA) return "solid";
    }
    const background = parseColor(s.backgroundColor);
    return background !== null && background[3] >= MIN_COVER_ALPHA ? "solid" : null;
  }

  // Whether something solid is painted over box at the viewport point x, y.
  // Fixed and sticky elements don't count: the page scrolls under them, so
  // what a header, a banner or a dialog covers at one step shows at another.
  function pointCover(box, x, y, pass) {
    const stack = stackAt(box, x, y);
    const index = stack.indexOf(box);
    // Hit testing skipped box, which ignores the pointer, so what's over it can't be told.
    if (index < 0) return VISIBLE;
    let unknown = false;
    for (let above = 0; above < index; above += 1) {
      const node = stack[above];
      if (pass.pinned(node)) continue;
      const paint = coverPaint(node, pass);
      if (paint === "solid") return COVERED;
      if (paint === "picture") unknown = true;
    }
    return unknown ? UNKNOWN : VISIBLE;
  }

  // VISIBLE when hit testing finds a point of rects with nothing solid over
  // it, COVERED when something solid is over every point it can try, and
  // OFF_SCREEN when no point is on screen to try. Points under a picture
  // count as visible, and element goes in unjudgedCover.
  function coverOf(element, box, rects, pass) {
    let tried = false;
    let unknown = false;
    for (const rect of rects) {
      for (const [across, down] of COVER_POINTS) {
        const x = rect.left + rect.width * across;
        const y = rect.top + rect.height * down;
        if (!onScreen(x, y)) continue;
        tried = true;
        const cover = pointCover(box, x, y, pass);
        if (cover === VISIBLE) return VISIBLE;
        if (cover === UNKNOWN) unknown = true;
      }
    }
    if (!tried) return OFF_SCREEN;
    if (unknown) {
      unjudgedCover.add(element);
      return VISIBLE;
    }
    unjudgedCover.delete(element);
    return COVERED;
  }

  // The first few boxes of element's own text.
  function ownTextBoxes(element) {
    const boxes = [];
    for (const node of ownTextNodes(element)) {
      for (const rect of textBoxes(node)) {
        boxes.push(rect);
        if (boxes.length >= MAX_COVER_BOXES) return boxes;
      }
    }
    return boxes;
  }

  function anyOnScreen(rects) {
    return rects.some(
      (rect) => rect.right > 0 && rect.bottom > 0 && rect.left < window.innerWidth && rect.top < window.innerHeight,
    );
  }

  // What this check makes of element's own text: VISIBLE, OFF_SCREEN,
  // COVERED or HIDDEN.
  function verdict(element, pass) {
    if (inspect(element, pass, true).length > 0) return HIDDEN;
    const s = pass.style(element);
    const box = s.display === "contents" ? nearestBoxedAncestor(element, pass) : element;
    return coverOf(element, box, ownTextBoxes(element), pass);
  }

  // Why a reader can't see element's own text; an empty list means they can.
  // With firstOnly it stops at the first reason, which is all a yes/no needs.
  function inspect(element, pass, firstOnly) {
    const s = pass.style(element);
    const box = s.display === "contents" ? nearestBoxedAncestor(element, pass) : element;
    if (box === null || !box.checkVisibility()) return [notRenderedReason(element, pass)];

    const reasons = [];
    const add = (reason) => {
      if (!reasons.includes(reason)) reasons.push(reason);
      return firstOnly;
    };
    // content-visibility:hidden on the element itself skips its contents, its own text included.
    if (s.contentVisibility === "hidden" && add(contentVisibilityReason(element))) return reasons;
    const ownBox = box === element;
    if (!box.checkVisibility(ownBox ? VISIBILITY_OPTIONS : BOX_OPTIONS)) {
      for (const reason of styleReasons(box, pass, ownBox)) {
        if (add(reason)) return reasons;
      }
    }
    const opacity = combinedOpacity(box, pass);
    if (opacity > 0 && opacity < MIN_VISIBLE_OPACITY && add("near-zero opacity")) return reasons;
    if ((s.visibility === "hidden" || s.visibility === "collapse") && add("visibility:hidden")) return reasons;
    if (!(parseFloat(s.fontSize) > 0) && add("font-size:0")) return reasons;
    const geometry = geometryReason(element, pass);
    if (geometry !== null) add(geometry);
    if (reasons.length === 0) {
      const contrast = contrastReason(element, pass);
      if (contrast !== null) add(contrast);
    }
    return reasons;
  }

  // Why a reader can't see element's box, for an element that shows
  // something other than text, such as an iframe; an empty list means they can.
  function boxReasons(element, pass) {
    if (!element.checkVisibility()) return [notRenderedReason(element, pass)];
    if (!element.checkVisibility(VISIBILITY_OPTIONS)) return styleReasons(element, pass, true);
    if (combinedOpacity(element, pass) < MIN_VISIBLE_OPACITY) return ["near-zero opacity"];
    const rect = element.getBoundingClientRect();
    if (rect.width < MIN_READABLE_PX || rect.height < MIN_READABLE_PX) return ["too small to see"];
    const blocked = reach(element, rect, pass, true);
    return blocked === null ? [] : [blocked];
  }

  // Records which frame owners have been seen with a box a reader can see.
  function trackFrameOwners(owners, pass) {
    for (const owner of owners) {
      let state = frameOwners.get(owner);
      if (state === undefined) {
        state = { visible: false, offScreen: false, covered: false };
        frameOwners.set(owner, state);
      }
      if (state.visible || boxReasons(owner, pass).length > 0) continue;
      const cover = coverOf(owner, owner, [owner.getBoundingClientRect()], pass);
      if (cover === VISIBLE) state.visible = true;
      else if (cover === OFF_SCREEN) state.offScreen = true;
      else state.covered = true;
    }
  }

  // An element is worth centring when it has a box and every reason it is
  // hidden is one a scroll animation could undo.
  function isCentrable(element, pass) {
    if (element.getClientRects().length === 0) return false;
    const reasons = inspect(element, pass, false);
    return reasons.length > 0 && reasons.every((reason) => CENTRABLE_REASONS.has(reason));
  }

  // Whether element's own text shows now, as far as this check can tell:
  // text with nothing on screen to hit-test counts as showing.
  function visibleNow(element, pass) {
    const seenNow = verdict(element, pass);
    return seenNow === VISIBLE || seenNow === OFF_SCREEN;
  }

  // A text element's record: whether its text showed at the first check
  // (null when it wasn't in the page yet), whether it showed at a later one,
  // whether it was visible every other way while off screen, at the first
  // check or later, and whether something solid was ever seen over it.
  function newEntry(atLoad) {
    return { atLoad, later: false, offScreenAtLoad: false, offScreenLater: false, covered: false };
  }

  // How a text element ended up: "visible at load", "revealed" (hidden at the
  // first check and seen later), "appeared" (added after load and seen) or
  // "hidden". Text never on screen to hit-test counts as seen the way the
  // other checks saw it, unless something solid was seen over it.
  function outcome(entry) {
    if (entry.atLoad === true) return "visible at load";
    if (entry.later) return entry.atLoad === false ? "revealed" : "appeared";
    if (entry.covered) return "hidden";
    if (entry.offScreenAtLoad) return "visible at load";
    if (entry.offScreenLater) return entry.atLoad === false ? "revealed" : "appeared";
    return "hidden";
  }

  // Updates entry with what this check makes of element. Text visible but
  // off screen at the first check, and then seen on screen with nothing
  // over it, counts as visible at load.
  function record(entry, element, pass, first) {
    const seenNow = verdict(element, pass);
    if (seenNow === VISIBLE) {
      if (first || entry.offScreenAtLoad) entry.atLoad = true;
      else entry.later = true;
    } else if (seenNow === OFF_SCREEN) {
      if (first) entry.offScreenAtLoad = true;
      else entry.offScreenLater = true;
    } else if (seenNow === COVERED) {
      entry.covered = true;
    }
  }

  function chooseScrollTarget(pass) {
    const root = document.scrollingElement || document.documentElement;
    if (pass.viewport.scrollsY && root.scrollHeight > root.clientHeight + 1) return root;
    // Some apps lock the page itself and scroll an inner panel instead.
    let best = null;
    let bestArea = 0;
    for (const element of document.body === null ? [] : document.body.querySelectorAll("*")) {
      if (element.scrollHeight <= element.clientHeight + 1) continue;
      const overflowY = pass.style(element).overflowY;
      if (overflowY !== "auto" && overflowY !== "scroll") continue;
      const area = element.clientWidth * element.clientHeight;
      if (area > bestArea) {
        best = element;
        bestArea = area;
      }
    }
    return best;
  }

  function nextFrame() {
    // requestAnimationFrame can stall in a page that isn't being drawn, so a
    // timer stands in for it.
    return new Promise((resolve) => {
      const timer = setTimeout(resolve, 100);
      requestAnimationFrame(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  function delay(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, milliseconds)));
  }

  function endTime(animation) {
    return animation.effect === null ? Infinity : animation.effect.getComputedTiming().endTime;
  }

  // An element's text with all whitespace removed, which is how the static
  // scan's text (strings joined by spaces) and the live text (joined by
  // nothing) can be compared. Script and style text is left out on both sides.
  function textKey(element) {
    if (element.querySelector("script, style, template") === null) return element.textContent.replace(/\s+/g, "");
    let key = "";
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      if (node.parentElement.closest("script, style, template") === null) key += node.data.replace(/\s+/g, "");
    }
    return key;
  }

  // The text elements of every live element that matches a static candidate
  // now, grouped by candidate. Only elements the scanner has seen count.
  function copiesNow() {
    const copies = Array.from({ length: staticCount }, () => []);
    for (const element of document.body === null ? [] : document.body.querySelectorAll("*")) {
      if (!staticTags.has(element.localName)) continue;
      const indexes = staticWanted.get(element.localName + "|" + textKey(element));
      if (indexes === undefined) continue;
      const members = [element, ...element.querySelectorAll("*")].filter((node) => seen.has(node));
      for (const index of indexes) copies[index].push(members);
    }
    return copies;
  }

  function setStaticCandidates(candidates) {
    staticWanted = new Map();
    staticKeys = candidates.map((candidate) => candidate.tag + "|" + candidate.text.replace(/\s+/g, ""));
    staticKeys.forEach((key, index) => {
      if (!staticWanted.has(key)) staticWanted.set(key, []);
      staticWanted.get(key).push(index);
    });
    staticTags = new Set(candidates.map((candidate) => candidate.tag));
    staticCount = candidates.length;
    // A reduce, not Math.max(...), which a page with enough candidates could overflow.
    longestStaticText = staticKeys.reduce((longest, key) => Math.max(longest, key.length - key.indexOf("|") - 1), 0);
  }

  // Before the first paint a reader sees nothing, and a stylesheet that
  // hides a copy may not have arrived yet, so nothing counts as seen until then.
  function painted() {
    if (!hasPainted) hasPainted = performance.getEntriesByType("paint").length > 0;
    return hasPainted;
  }

  function watchWithText(element, text) {
    const indexes = staticWanted.get(element.localName + "|" + text);
    if (indexes === undefined) return;
    for (const index of indexes) watchedCopies[index].add(element);
  }

  function watchSubtree(root) {
    for (const element of [root, ...root.querySelectorAll("*")]) {
      if (staticTags.has(element.localName)) watchWithText(element, textKey(element));
    }
  }

  // New text inside element changes the text of element and its ancestors.
  // An ancestor holds at least the text of its descendants, so the walk stops
  // once the text is longer than any candidate's.
  function watchAncestors(element) {
    for (let node = element; node !== null && node !== document.documentElement; node = node.parentElement) {
      const text = textKey(node);
      if (text.length > longestStaticText) return;
      if (staticTags.has(node.localName)) watchWithText(node, text);
    }
  }

  function sampleWatched() {
    if (!painted()) return;
    watchSamples += 1;
    const now = performance.now();
    let pass = null;
    for (let index = 0; index < staticCount; index += 1) {
      if (firstVisibleAt[index] !== null) continue;
      for (const element of watchedCopies[index]) {
        // A node can be removed, or keep its place and change its words.
        if (!element.isConnected || element.localName + "|" + textKey(element) !== staticKeys[index]) {
          watchedCopies[index].delete(element);
          continue;
        }
        if (pass === null) pass = newPass();
        if (collectTextElements(element).some((member) => visibleNow(member, pass))) {
          firstVisibleAt[index] = now;
          break;
        }
      }
    }
  }

  function watchMutations(records) {
    // Many records can share a parent, so each grown element is walked once.
    const grown = new Set();
    for (const record of records) {
      if (!record.target.isConnected) continue;
      if (record.type === "characterData") {
        if (record.target.parentElement !== null) grown.add(record.target.parentElement);
        continue;
      }
      if (record.addedNodes.length === 0) continue;
      for (const node of record.addedNodes) {
        if (node.nodeType === Node.ELEMENT_NODE && node.isConnected) watchSubtree(node);
      }
      if (record.target.nodeType === Node.ELEMENT_NODE) grown.add(record.target);
    }
    for (const element of grown) watchAncestors(element);
  }

  // Checked at the next frame rather than at once, so a node a script hides
  // again before the browser paints doesn't count as seen.
  function sampleAtNextFrame() {
    if (sampleQueued) return;
    sampleQueued = true;
    nextFrame().then(
      guarded(() => {
        sampleQueued = false;
        if (watching) sampleWatched();
      }),
    );
  }

  function stopWatching() {
    watching = false;
    if (watchTimer !== null) clearInterval(watchTimer);
    watchTimer = null;
    if (watchObserver !== null) watchObserver.disconnect();
    watchObserver = null;
  }

  // An error inside a timer or an observer would otherwise vanish, and the
  // trace would quietly lose what the watch was there to catch.
  function guarded(work) {
    return (...args) => {
      if (watchError !== null) return;
      try {
        work(...args);
      } catch (error) {
        watchError = String(error instanceof Error ? error.message : error);
        stopWatching();
      }
    };
  }

  // Each text element belongs to exactly one unit, so no text is quoted twice.
  function textOf(root, rootOf) {
    const pieces = [];
    forEachComposed(root, (node) => {
      if (node.nodeType !== Node.TEXT_NODE || rootOf.get(ownerOf(node)) !== root) return;
      const text = node.data.trim();
      if (text !== "") pieces.push(text);
    });
    return pieces.join(" ");
  }

  const scanner = {
    // Starts watching the static candidates ({tag, text}) for the rest of
    // the scan. Returns how many there are.
    watchStatic(candidates) {
      stopWatching();
      setStaticCandidates(candidates);
      watchedCopies = Array.from({ length: staticCount }, () => new Set());
      firstVisibleAt = new Array(staticCount).fill(null);
      if (staticCount === 0) return staticCount;
      guarded(() => {
        if (document.body !== null) watchSubtree(document.body);
        sampleWatched();
      })();
      if (watchError !== null) return staticCount;
      watching = true;
      watchObserver = new MutationObserver(
        guarded((records) => {
          watchMutations(records);
          sampleAtNextFrame();
        }),
      );
      watchObserver.observe(document, { childList: true, subtree: true, characterData: true });
      watchTimer = setInterval(guarded(sampleWatched), WATCH_INTERVAL_MS);
      return staticCount;
    },

    watchStatus() {
      return { samples: watchSamples, error: watchError };
    },

    recordLoad() {
      firstCheckAt = performance.now();
      const pass = newPass();
      const { elements, owners } = readTree();
      for (const element of elements) {
        const entry = newEntry(false);
        record(entry, element, pass, true);
        seen.set(element, entry);
      }
      trackFrameOwners(owners, pass);
      scrollTarget = chooseScrollTarget(pass);
      return seen.size;
    },

    // Takes closed shadow roots render.py found, so their text is checked
    // like any other. Returns how many it hadn't been given before.
    adoptShadowRoots(...roots) {
      let added = 0;
      for (const root of roots) {
        if (!(root instanceof ShadowRoot) || closedRoots.get(root.host) === root) continue;
        closedRoots.set(root.host, root);
        added += 1;
      }
      return added;
    },

    // Why a reader can't see the box of owner, an element holding a frame,
    // with its tag. No reasons when any check saw it.
    frameOwnerReasons(owner) {
      const state = frameOwners.get(owner);
      if (state !== undefined && state.visible) return { tag: owner.localName, reasons: [] };
      const pass = newPass();
      const reasons = boxReasons(owner, pass);
      if (reasons.length > 0) return { tag: owner.localName, reasons };
      const cover = coverOf(owner, owner, [owner.getBoundingClientRect()], pass);
      // A box off screen whenever it was checked counts as showing, unless something solid was seen over it.
      const covered = cover === COVERED || (state !== undefined && state.covered && cover !== VISIBLE);
      return { tag: owner.localName, reasons: covered ? [COVERED_REASON] : [] };
    },

    // Brings owner, an element holding a frame, to the middle of the
    // viewport, where the frame's own scroll animations can fire.
    showFrameOwner(owner) {
      owner.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
      return true;
    },

    // The own text of every text element in this document, up to limit
    // characters, for a frame whose box a reader can't see.
    frameText(limit) {
      const pieces = [];
      let size = 0;
      const elements = collectTextElements();
      for (const element of elements) {
        for (const node of ownTextNodes(element)) {
          const text = node.data.trim();
          if (text === "") continue;
          pieces.push(text);
          size += text.length + 1;
          if (size > limit) return { text: pieces.join(" ").slice(0, limit), elements: elements.length };
        }
      }
      return { text: pieces.join(" "), elements: elements.length };
    },

    // The document's content type, and its text when it's plain text, so a
    // frame that shows only loupe's own refusal can be told apart.
    documentKind(limit) {
      const plain = document.contentType === "text/plain" && document.body !== null;
      return { contentType: document.contentType, text: plain ? document.body.textContent.slice(0, limit) : "" };
    },

    // Pairs each candidate passed to watchStatic() with every live element of
    // the same tag and text. Run right after recordLoad(); compareStatic()
    // matches against the final page as well. Returns how many candidates
    // found at least one copy.
    matchStatic() {
      staticCopies = copiesNow();
      return staticCopies.filter((copies) => copies.length > 0).length;
    },

    // For each candidate, for each copy matched at load or in the final page,
    // the state of each of its text elements. A framework can replace a node
    // after load, so the final page may hold the copy a reader actually saw.
    // A copy the watch saw visible adds one more state: visible at load if
    // that was at or before the first check, revealed if after it.
    compareStatic() {
      const state = (element) => {
        const ended = outcome(seen.get(element));
        if (ended === "visible at load") return ended;
        // Seen later, whether revealed by scrolling or added after load.
        return ended === "hidden" ? "still hidden" : "revealed on scroll";
      };
      const finalCopies = copiesNow();
      return staticCopies.map((copies, index) => {
        const states = copies.concat(finalCopies[index]).map((members) => members.map(state));
        const visibleAt = firstVisibleAt[index] ?? null;
        if (visibleAt !== null) {
          states.push([firstCheckAt !== null && visibleAt <= firstCheckAt ? "visible at load" : "revealed on scroll"]);
        }
        return states;
      });
    },

    recheck() {
      const pass = newPass();
      const { elements, owners } = readTree();
      for (const element of elements) {
        let entry = seen.get(element);
        if (entry === undefined) {
          entry = newEntry(null);
          seen.set(element, entry);
        }
        if (entry.atLoad === true || entry.later) continue;
        // Text already seen visible but off screen needs checking again only once some of it is on screen.
        if ((entry.offScreenAtLoad || entry.offScreenLater) && !anyOnScreen(ownTextBoxes(element))) continue;
        record(entry, element, pass, false);
      }
      trackFrameOwners(owners, pass);
    },

    scrollToTop() {
      if (scrollTarget !== null) scrollTarget.scrollTo({ top: 0, behavior: "instant" });
    },

    // Scrolls down by one screen and reports whether that reached the bottom.
    scrollOneViewport() {
      if (scrollTarget === null) return true;
      const before = scrollTarget.scrollTop;
      scrollTarget.scrollTo({ top: before + scrollTarget.clientHeight, behavior: "instant" });
      return scrollTarget.scrollTop <= before || scanner.atBottom();
    },

    // Some reveals fire only when an element reaches one position, such as an
    // observer watching a band in the middle of the viewport, and screen-sized
    // steps can skip that position. This scrolls the next still-hidden element
    // that has a box to the middle of the viewport. Returns false when none is
    // left. Elements without a box (display:none) can't be moved into view.
    centreNext() {
      const pass = newPass();
      for (const element of collectTextElements()) {
        const entry = seen.get(element);
        if (entry === undefined || entry.atLoad === true || entry.later || centred.has(element)) continue;
        centred.add(element);
        if (!isCentrable(element, pass)) continue;
        element.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
        return true;
      }
      return false;
    },

    uncentredCount() {
      const pass = newPass();
      let count = 0;
      for (const element of collectTextElements()) {
        const entry = seen.get(element);
        if (entry === undefined || entry.atLoad === true || entry.later || centred.has(element)) continue;
        if (isCentrable(element, pass)) count += 1;
      }
      return count;
    },

    // The document's text as it stands, outside scripts, styles, templates
    // and noscript, up to limit characters, so the HTML as served can be
    // checked for text the page's scripts took out.
    pageText(limit) {
      const parts = [];
      let size = 0;
      const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
        const parent = node.parentElement;
        if (parent !== null && parent.closest("script, style, template, noscript") !== null) continue;
        parts.push(node.data);
        size += node.data.length + 1;
        if (size > limit) return { text: "", complete: false };
      }
      return { text: parts.join(" "), complete: true };
    },

    atBottom() {
      if (scrollTarget === null) return true;
      return scrollTarget.scrollTop + scrollTarget.clientHeight >= scrollTarget.scrollHeight - 2;
    },

    // Waits for running transitions and animations that have an end. Returns
    // false when some were still running at the deadline.
    async settle(maxMilliseconds) {
      const deadline = performance.now() + maxMilliseconds;
      await nextFrame();
      await nextFrame();
      while (performance.now() < deadline) {
        const running = document
          .getAnimations()
          .filter((animation) => animation.playState === "running" && Number.isFinite(endTime(animation)));
        if (running.length === 0) return true;
        await Promise.race([
          Promise.allSettled(running.map((animation) => animation.finished)),
          delay(deadline - performance.now()),
        ]);
        await nextFrame();
      }
      return false;
    },

    finish() {
      // From here the final check covers what the watch would see.
      stopWatching();
      scanner.recheck();
      const pass = newPass();
      const elements = collectTextElements();
      const counts = {
        text_elements: elements.length,
        visible_at_load: 0,
        revealed_on_scroll: 0,
        appeared_after_load: 0,
        hidden: 0,
      };
      const bucketOf = new Map();
      for (const element of elements) {
        const ended = outcome(seen.get(element));
        let bucket;
        if (ended === "visible at load") {
          bucket = "visible";
          counts.visible_at_load += 1;
        } else if (ended === "revealed") {
          bucket = "revealed";
          counts.revealed_on_scroll += 1;
        } else if (ended === "appeared") {
          bucket = "visible";
          counts.appeared_after_load += 1;
        } else {
          bucket = "hidden";
          counts.hidden += 1;
        }
        bucketOf.set(element, bucket);
      }

      // Tally each ancestor's text elements by bucket, so a run of hidden or
      // revealed text is reported once, on the outermost element it fills.
      const tallies = new Map();
      for (const element of elements) {
        const bucket = bucketOf.get(element);
        for (let node = element; node !== null && node !== document.documentElement; node = node.parentElement) {
          let tally = tallies.get(node);
          if (tally === undefined) {
            tally = { total: 0, visible: 0, revealed: 0, hidden: 0 };
            tallies.set(node, tally);
          }
          tally.total += 1;
          tally[bucket] += 1;
        }
      }

      const units = new Map();
      const rootOf = new Map();
      for (const element of elements) {
        const bucket = bucketOf.get(element);
        if (bucket === "visible") continue;
        let root = element;
        for (
          let parent = element.parentElement;
          parent !== null && parent !== document.body && parent !== document.documentElement;
          parent = parent.parentElement
        ) {
          const tally = tallies.get(parent);
          if (tally[bucket] !== tally.total) break;
          root = parent;
        }
        rootOf.set(element, root);
        let unit = units.get(root);
        if (unit === undefined) {
          unit = { bucket, reasons: [], shadow: false };
          units.set(root, unit);
        }
        if (element.getRootNode() instanceof ShadowRoot || shadowOwned.has(element)) unit.shadow = true;
        if (bucket === "hidden") {
          const reasons = inspect(element, pass, false);
          if (seen.get(element).covered) reasons.push(COVERED_REASON);
          for (const reason of reasons) {
            if (!unit.reasons.includes(reason)) unit.reasons.push(reason);
          }
        }
      }

      const hidden_units = [];
      const revealed_units = [];
      for (const [root, unit] of units) {
        const item = { tag: root.localName, text: textOf(root, rootOf), shadow: unit.shadow };
        if (unit.bucket === "hidden") hidden_units.push({ ...item, reasons: unit.reasons });
        else revealed_units.push(item);
      }
      // Images loaded over http(s) are requests a remote run's blocking should have refused.
      const fetched_images = Array.from(document.images).filter((image) =>
        /^https?:/i.test(image.currentSrc || image.src),
      ).length;
      const viewport = [window.innerWidth, window.innerHeight];
      const unjudged_contrast = elements.filter((element) => unjudged.has(element)).length;
      const unjudged_cover = elements.filter((element) => unjudgedCover.has(element)).length;
      return { ...counts, viewport, fetched_images, unjudged_contrast, unjudged_cover, hidden_units, revealed_units };
    },
  };

  globalThis.loupeScanner = scanner;
  return true;
})();
