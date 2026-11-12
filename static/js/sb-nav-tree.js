/* Multi-level collapsible sidebar navigation.
 * Operates purely on the .ni-group/.ni-row/.ni-chev/.ni-children structure
 * rendered by templates/dashboard/index.html's sidebar block - does not
 * know about, and never touches, tab-switching (go()) or routing. Expand/
 * collapse state is independent of which tab is active.
 */
(function () {
  var STORE_KEY = 'sb_nav_expanded';

  function loadExpanded() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(parsed) ? parsed : []);
    } catch (_) {
      return new Set();
    }
  }

  function saveExpanded(set) {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(Array.from(set)));
    } catch (_) {}
  }

  var expanded = loadExpanded();

  function groupFor(nodeId) {
    return document.querySelector('.ni-group[data-ni-node="' + CSS.escape(nodeId) + '"]');
  }

  function setGroupState(group, open, opts) {
    if (!group) return;
    var nodeId = group.getAttribute('data-ni-node');
    var chev = group.querySelector(':scope > .ni-row > .ni-chev');
    group.classList.toggle('ni-open', open);
    if (chev) chev.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (nodeId) {
      if (open) expanded.add(nodeId); else expanded.delete(nodeId);
      if (!(opts && opts.silent)) saveExpanded(expanded);
    }
  }

  function toggleGroup(nodeId) {
    var group = groupFor(nodeId);
    if (!group) return;
    setGroupState(group, !group.classList.contains('ni-open'));
  }

  function expandAncestors(el) {
    var group = el.closest('.ni-group');
    while (group) {
      setGroupState(group, true, { silent: false });
      group = group.parentElement ? group.parentElement.closest('.ni-group') : null;
    }
  }

  function restoreExpandedState() {
    document.querySelectorAll('.ni-group[data-ni-node]').forEach(function (group) {
      var nodeId = group.getAttribute('data-ni-node');
      if (expanded.has(nodeId)) setGroupState(group, true, { silent: true });
    });
  }

  function expandActiveAncestors() {
    var activeItem = document.querySelector('.sb-nav .ni-group .ni.active, .sb-nav .ni-group a.ni.active');
    if (activeItem) expandAncestors(activeItem);
  }

  function bindChevrons() {
    document.querySelectorAll('.ni-chev[data-ni-toggle]').forEach(function (btn) {
      if (btn._sbNavBound) return;
      btn._sbNavBound = true;
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        toggleGroup(btn.getAttribute('data-ni-toggle'));
      });
    });
  }

  function init() {
    bindChevrons();
    restoreExpandedState();
    expandActiveAncestors();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Re-run whenever a tab switch changes which .ni is active (go() dispatches
  // this event already), so navigating into a nested page keeps its parents
  // expanded without a full page reload.
  document.addEventListener('fg:tab-changed', function () {
    bindChevrons();
    expandActiveAncestors();
  });

  window.sbNavTree = { toggleGroup: toggleGroup, expandAncestors: expandAncestors };
})();
