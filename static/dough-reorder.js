window.AlphaDoughReorder = (container, {onFinish, onStart = () => {}, disabled = () => false}) => {
  let gesture = null, timer = null, frame = null;
  const cards = () => [...container.querySelectorAll('[data-stock-index]')];
  function move() {
    if (!gesture?.active) return;
    const {card, x} = gesture;
    const bounds = container.getBoundingClientRect();
    if (x < bounds.left + 30) container.scrollLeft -= 8;
    if (x > bounds.right - 30) container.scrollLeft += 8;
    const target = cards().filter(item => item !== card).find(item => {
      const rect = item.getBoundingClientRect(); return x < rect.left + rect.width / 2;
    });
    if (target) container.insertBefore(card, target); else container.append(card);
    frame = requestAnimationFrame(move);
  }
  function activate() {
    if (!gesture || gesture.active) return;
    gesture.active = true; gesture.card.classList.add('is-reordering'); onStart();
    try { navigator.vibrate?.(25); } catch (_) {}
    move();
  }
  function finish(cancelled) {
    if (!gesture) return;
    clearTimeout(timer); cancelAnimationFrame(frame);
    const current = gesture; gesture = null;
    current.card.classList.remove('is-reordering');
    if (cancelled) current.original.forEach(card => container.append(card));
    if (container.hasPointerCapture(current.id)) container.releasePointerCapture(current.id);
    if (current.active) onFinish(!cancelled);
  }
  container.addEventListener('pointerdown', event => {
    const card = event.target.closest('[data-stock-index]');
    if (!card || !container.contains(card) || disabled() || gesture || event.button !== 0) return;
    event.preventDefault();
    gesture = {card, id:event.pointerId, x:event.clientX, startX:event.clientX,
      startY:event.clientY, lastX:event.clientX, active:false, mouse:event.pointerType === 'mouse', original:cards()};
    // Capture on the stable container: moving a card must not lose the pointer.
    container.setPointerCapture(event.pointerId);
    timer = setTimeout(activate, 400);
  });
  container.addEventListener('pointermove', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    event.preventDefault(); gesture.x = event.clientX;
    if (!gesture.active && Math.hypot(event.clientX-gesture.startX,event.clientY-gesture.startY)>10) {
      clearTimeout(timer);
      if (gesture.mouse) activate();
      else container.scrollLeft -= event.clientX-gesture.lastX;
    }
    gesture.lastX=event.clientX;
  });
  container.addEventListener('pointerup', event => {if(gesture?.id===event.pointerId)finish(false)});
  container.addEventListener('pointercancel', event => {if(gesture?.id===event.pointerId)finish(true)});
  container.addEventListener('lostpointercapture', () => finish(true));
  container.addEventListener('contextmenu', event => event.preventDefault());
  container.addEventListener('dragstart', event => event.preventDefault());
  container.addEventListener('keydown', event => {
    const card=event.target.closest('[data-stock-index]');
    if (!card || disabled() || !event.altKey || !['ArrowLeft','ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const sibling=event.key==='ArrowLeft'?card.previousElementSibling:card.nextElementSibling;
    if (!sibling) return;
    if(event.key==='ArrowLeft')container.insertBefore(card,sibling);else container.insertBefore(sibling,card);
    card.focus();onFinish(true);
  });
};
