const modal = document.querySelector("#action-modal");
const modalTitle = document.querySelector("#modal-title");
const modalSubtitle = document.querySelector("#modal-subtitle");
const form = document.querySelector("#action-form");
const success = document.querySelector("#modal-success");
const intentSelect = document.querySelector("select[name='intent']");

const actionCopy = {
  demo: {
    title: "Richiedi una demo",
    subtitle: "Mostraci il tuo locale e creiamo insieme il menu digitale.",
  },
  signup: {
    title: "Inizia gratis",
    subtitle: "Crea il tuo primo menu digitale in meno di un minuto.",
  },
  order: {
    title: "Ordina dal tavolo",
    subtitle: "Attiva l'ordine diretto dal tavolo per i tuoi clienti.",
  },
  "plan-starter": {
    title: "Piano Starter",
    subtitle: "Ideale per locali che vogliono digitalizzare il menu.",
  },
  "plan-pro": {
    title: "Piano Pro",
    subtitle: "Per team che desiderano ordini e analisi avanzate.",
  },
  contact: {
    title: "Parla con noi",
    subtitle: "Raccontaci le tue esigenze: ti suggeriamo la soluzione migliore.",
  },
  login: {
    title: "Accesso area clienti",
    subtitle: "Inserisci i dati e ti inviamo le credenziali di accesso.",
  },
};

const openModal = (action) => {
  const copy = actionCopy[action] ?? actionCopy.demo;
  modalTitle.textContent = copy.title;
  modalSubtitle.textContent = copy.subtitle;
  intentSelect.value = action in actionCopy ? action : "demo";
  form.reset();
  success.hidden = true;
  form.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  modal.classList.add("is-visible");
};

const closeModal = () => {
  modal.setAttribute("aria-hidden", "true");
  modal.classList.remove("is-visible");
};

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    openModal(button.dataset.action);
  });
});

modal.addEventListener("click", (event) => {
  if (event.target instanceof HTMLElement && event.target.dataset.close === "true") {
    closeModal();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && modal.classList.contains("is-visible")) {
    closeModal();
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  form.hidden = true;
  success.hidden = false;
});
