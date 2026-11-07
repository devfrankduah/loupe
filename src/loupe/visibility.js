// The in-browser half of loupe's rendered scan. render.py runs this in a
// JavaScript world of its own on the page's main frame: page scripts share
// the DOM with it but not its globals or prototypes, so a page can't patch
// checkVisibility or getClientRects to make hidden text look visible.
//
// It installs one object, loupeScanner. render.py calls recordLoad() once the
// page has loaded, recheck() after every scroll step and every centreNext(),
// and finish() at the end.
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

  const VISIBILITY_OPTIONS = { opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true };
  // A display:contents element has no box of its own, so its nearest boxed
  // ancestor is checked instead, and the element's own visibility separately.
  const BOX_OPTIONS = { opacityProperty: true, contentVisibilityAuto: true };
  const NO_OVERFLOW_DISPLAYS = new Set(["inline", "contents", "none"]);

  // Element -> { atLoad: true | false | null, later: boolean }. atLoad is null
  // for elements that weren't in the page yet when it finished loading.
  const seen = new Map();
  let scrollTarget = null;
  // Elements centreNext() has already tried, or found to have no box.
  const centred = new Set();
  // For each static candidate passed to matchStatic(), the text elements of
  // every live element with the same tag and text, as matched at load.
  let staticCopies = [];
  let staticWanted = new Map();
  let staticTags = new Set();
  let staticCount = 0;

  function collectTextElements() {
    const elements = [];
    if (document.body === null) return elements;
    const accepted = new Set();
    const rejected = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (parent === null || accepted.has(parent) || rejected.has(parent)) continue;
      if (node.data.trim() === "") continue;
      if (parent.closest(EXCLUDED) !== null) {
        rejected.add(parent);
        continue;
      }
      accepted.add(parent);
      elements.push(parent);
    }
    return elements;
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
    return { style, box, viewport: describeViewport(style) };
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

  // Follows one rectangle of text up through everything that can clip it.
  // Returns why a reader can't see it, or null when enough of it shows.
  function reach(element, rect, pass) {
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
    for (let node = element; node !== null && node !== document.documentElement; node = node.parentElement) {
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

  function geometryReason(element, pass) {
    let reason = "too small to see";
    for (const child of element.childNodes) {
      if (child.nodeType !== Node.TEXT_NODE) continue;
      const start = child.data.search(/\S/);
      if (start === -1) continue;
      const end = child.data.length - /\s*$/.exec(child.data)[0].length;
      const range = document.createRange();
      range.setStart(child, start);
      range.setEnd(child, end);
      for (const rect of range.getClientRects()) {
        if (rect.width < MIN_READABLE_PX || rect.height < MIN_READABLE_PX) continue;
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
    const enclosingDetails = (node) => node.parentElement?.closest("details") ?? null;
    for (let details = element.closest("details"); details !== null; details = enclosingDetails(details)) {
      if (details.open) continue;
      const summary = Array.from(details.children).find((child) => child.localName === "summary");
      if (summary === undefined || !summary.contains(element)) return "inside a closed <details>";
    }
    for (let node = element; node !== null; node = node.parentElement) {
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
    for (let node = box; node !== null; node = node.parentElement) {
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
    for (let node = box; node !== null; node = node.parentElement) {
      const s = pass.style(node);
      if (s.display === "contents") continue;
      const opacity = parseFloat(s.opacity);
      if (Number.isFinite(opacity)) product *= opacity;
      if (product < MIN_VISIBLE_OPACITY) break;
    }
    return product;
  }

  function nearestBoxedAncestor(element, pass) {
    for (let node = element.parentElement; node !== null; node = node.parentElement) {
      if (pass.style(node).display !== "contents") return node;
    }
    return null;
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
    return reasons;
  }

  function visibleNow(element, pass) {
    return inspect(element, pass, true).length === 0;
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

  // Each text element belongs to exactly one unit, so no text is quoted twice.
  function textOf(root, rootOf) {
    const pieces = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      if (rootOf.get(node.parentElement) !== root) continue;
      const text = node.data.trim();
      if (text !== "") pieces.push(text);
    }
    return pieces.join(" ");
  }

  const scanner = {
    recordLoad() {
      const pass = newPass();
      for (const element of collectTextElements()) {
        seen.set(element, { atLoad: visibleNow(element, pass), later: false });
      }
      scrollTarget = chooseScrollTarget(pass);
      return seen.size;
    },

    // Pairs each static candidate ({tag, text}) with every live element of the
    // same tag and text. Run right after recordLoad(), before scripts move or
    // replace nodes. Returns how many candidates found at least one copy.
    // Pairs each static candidate ({tag, text}) with every live element of the
    // same tag and text. Run right after recordLoad(), before scripts move or
    // replace nodes; compareStatic() matches against the final page as well.
    // Returns how many candidates found at least one copy.
    matchStatic(candidates) {
      staticWanted = new Map();
      candidates.forEach((candidate, index) => {
        const key = candidate.tag + "|" + candidate.text.replace(/\s+/g, "");
        if (!staticWanted.has(key)) staticWanted.set(key, []);
        staticWanted.get(key).push(index);
      });
      staticTags = new Set(candidates.map((candidate) => candidate.tag));
      staticCount = candidates.length;
      staticCopies = copiesNow();
      return staticCopies.filter((copies) => copies.length > 0).length;
    },

    // For each candidate, for each copy matched at load or in the final page,
    // the state of each of its text elements. A framework can replace a node
    // after load, so the final page may hold the copy a reader actually saw.
    compareStatic() {
      const state = (element) => {
        const entry = seen.get(element);
        if (entry.atLoad === true) return "visible at load";
        // Seen later, whether revealed by scrolling or added after load.
        return entry.later ? "revealed on scroll" : "still hidden";
      };
      const finalCopies = copiesNow();
      return staticCopies.map((copies, index) =>
        copies.concat(finalCopies[index]).map((members) => members.map(state)),
      );
    },

    recheck() {
      const pass = newPass();
      for (const element of collectTextElements()) {
        let entry = seen.get(element);
        if (entry === undefined) {
          entry = { atLoad: null, later: false };
          seen.set(element, entry);
        }
        if (entry.atLoad !== true && !entry.later) entry.later = visibleNow(element, pass);
      }
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
      for (const element of collectTextElements()) {
        const entry = seen.get(element);
        if (entry === undefined || entry.atLoad === true || entry.later || centred.has(element)) continue;
        centred.add(element);
        if (element.getClientRects().length === 0) continue;
        element.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
        return true;
      }
      return false;
    },

    uncentredCount() {
      let count = 0;
      for (const element of collectTextElements()) {
        const entry = seen.get(element);
        if (entry === undefined || entry.atLoad === true || entry.later || centred.has(element)) continue;
        if (element.getClientRects().length > 0) count += 1;
      }
      return count;
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
        const entry = seen.get(element);
        let bucket;
        if (entry.atLoad === true) {
          bucket = "visible";
          counts.visible_at_load += 1;
        } else if (entry.later && entry.atLoad === false) {
          bucket = "revealed";
          counts.revealed_on_scroll += 1;
        } else if (entry.later) {
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
          unit = { bucket, reasons: [] };
          units.set(root, unit);
        }
        if (bucket === "hidden") {
          for (const reason of inspect(element, pass, false)) {
            if (!unit.reasons.includes(reason)) unit.reasons.push(reason);
          }
        }
      }

      const hidden_units = [];
      const revealed_units = [];
      for (const [root, unit] of units) {
        const item = { tag: root.localName, text: textOf(root, rootOf) };
        if (unit.bucket === "hidden") hidden_units.push({ ...item, reasons: unit.reasons });
        else revealed_units.push(item);
      }
      // Images loaded over http(s) are requests a remote run's blocking should have refused.
      const fetched_images = Array.from(document.images).filter((image) =>
        /^https?:/i.test(image.currentSrc || image.src),
      ).length;
      const viewport = [window.innerWidth, window.innerHeight];
      return { ...counts, viewport, fetched_images, hidden_units, revealed_units };
    },
  };

  globalThis.loupeScanner = scanner;
  return true;
})();
