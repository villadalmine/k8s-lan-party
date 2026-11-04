// What's new (/whats-new): one asciinema player per recording; the player and the recordings come from this server only
"use strict";
document.querySelectorAll("[data-cast]").forEach(function (el) {
  AsciinemaPlayer.create(el.getAttribute("data-cast"), el, { fit: "width", idleTimeLimit: 2, poster: "npt:0:4", terminalFontSize: "small" });
});
