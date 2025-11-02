/* app/static/app.js */
// Templates/tabs
function initTemplates() {
  const templatesData = document.getElementById('templates-data');
  const templates = templatesData ? JSON.parse(templatesData.textContent) : {};
  const tabs = document.querySelectorAll('#template-tabs .template-tab');
  const textarea = document.getElementById('message_template');
  const hiddenInput = document.getElementById('template_name');
  const contactsBtn = document.getElementById('contacts-template-btn');

  const templateLinks = {
    new_rim: 'https://docs.google.com/spreadsheets/d/1B0f-6vyHJng4F6IAkEKHo7Y4o-p3H1vb/edit?usp=sharing&ouid=115733225928091106688&rtpof=true&sd=true',
    check_rim: 'https://docs.google.com/spreadsheets/d/1YZMw48rVNX3a73jUCeBghdMDPvJRzpAs/edit?usp=sharing&ouid=115733225928091106688&rtpof=true&sd=true',
    check: 'https://docs.google.com/spreadsheets/d/1B0f-6vyHJng4F6IAkEKHo7Y4o-p3H1vb/edit?usp=sharing&ouid=115733225928091106688&rtpof=true&sd=true',
    confirm: 'https://docs.google.com/spreadsheets/d/1YZMw48rVNX3a73jUCeBghdMDPvJRzpAs/edit?usp=sharing&ouid=115733225928091106688&rtpof=true&sd=true',
    media: 'https://docs.google.com/spreadsheets/d/1PehBtIvRblqi4eHgjdWezGKo9IRyisSS/edit?usp=sharing&ouid=115733225928091106688&rtpof=true&sd=true',
    close: 'https://docs.google.com/spreadsheets/d/1B0f-6vyHJng4F6IAkEKHo7Y4o-p3H1vb/edit?usp=sharing&ouid=115733225928091106688&rtpof=true&sd=true'
  };

  function setTemplate(templateKey) {
    if (!templates[templateKey]) return;
    textarea.value = templates[templateKey];
    hiddenInput.value = templateKey;
    if (contactsBtn) contactsBtn.onclick = () => window.open(templateLinks[templateKey], '_blank');
  }

  setTemplate('new_rim');

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const key = tab.dataset.value;
      setTemplate(key);
    });
  });
}

// Subject preview
function initSubjectPreview() {
  const brandInput = document.querySelector('input[name="brand"]');
  const periodInput = document.querySelector('input[name="period"]');
  const subjectPreviewBox = document.getElementById('subject-preview-box');

  function updateSubjectPreview() {
    const brand = brandInput.value.trim();
    const period = periodInput.value.trim();
    const firstRowDiv = document.getElementById('first-row-data');
    if (!firstRowDiv || !brand || !period) {
      subjectPreviewBox.style.display = 'none';
      return;
    }
    const mall = firstRowDiv.dataset.mall || '';
    const city = firstRowDiv.dataset.city || '';
    subjectPreviewBox.innerText = `${mall} (г. ${city}) // ${brand} // ${period}`;
    subjectPreviewBox.style.display = 'block';
  }

  window.updateSubjectPreview = updateSubjectPreview; // used after preview
  brandInput.addEventListener('input', updateSubjectPreview);
  periodInput.addEventListener('input', updateSubjectPreview);
}

// Preview Excel upload
let uploadedFile = null;
function previewExcel(file) {
  if (!file) return;
  const preview = document.getElementById('preview');
  preview.innerHTML = '<div class="loading-spinner"></div>';

  const formData = new FormData();
  formData.append("contacts_file", file);
  const addPrefix = document.getElementById("add-tc-prefix").checked;
  formData.append("add_tc_prefix", addPrefix);

  fetch("/preview-excel", { method: "POST", body: formData })
    .then(response => {
      if (!response.ok) throw new Error("Ошибка при чтении Excel");
      return response.text();
    })
    .then(html => {
      preview.innerHTML = html;
      preview.style.border = "none";
      preview.style.padding = "0";
      preview.style.minHeight = "0";
      if (window.updateSubjectPreview) window.updateSubjectPreview();
      const statusEl = document.getElementById('status');
      if (statusEl) statusEl.textContent = "Waiting...";
    })
    .catch(error => {
      preview.innerHTML = `<div style="color:red;">❌ ${error.message}</div>`;
      const statusEl = document.getElementById('status');
      if (statusEl) statusEl.textContent = "";
    });
}
function initPreviewHandlers() {
  const fileInput = document.getElementById("contacts_file");
  const addPrefix = document.getElementById("add-tc-prefix");
  if (fileInput) {
    // Clear the input value on click so choosing a file with the same name
    // will still fire the `change` event (browsers may not emit `change`
    // if the same filename is selected twice in a row).
    fileInput.addEventListener("click", e => {
      // Clear the element value to ensure the browser treats the next selection
      // as a change even if the filename is the same. Use empty string which
      // is more compatible across browsers than null.
      try { e.target.value = ''; uploadedFile = null; } catch (err) { /* ignore */ }
    });

    fileInput.addEventListener("change", e => {
      uploadedFile = e.target.files[0];
      previewExcel(uploadedFile);
    });
  }
  if (addPrefix) {
    addPrefix.addEventListener("change", () => {
      if (uploadedFile) previewExcel(uploadedFile);
    });
  }
  // keep global for existing inline handler if present
  window.handleFileChange = (file) => {
    uploadedFile = file;
    previewExcel(file);
  };
}

// Delayed submit with HTMX
function initDelayedSubmit() {
  const form = document.querySelector("form");
  const delayedBtn = document.getElementById("delayed-submit");
  const countdownBox = document.getElementById("countdown-box");
  const countdownText = document.getElementById("countdown-text");
  const cancelBtn = document.getElementById("cancel-submit");

  const contactsInput = document.getElementById("contacts_file");
  const brandInput = document.querySelector('input[name="brand"]');
  const periodInput = document.querySelector('input[name="period"]');
  const templateInput = document.getElementById("message_template");

  let countdownTimer;
  let secondsLeft = 5;

  function validateForm() {
    if (!contactsInput.files.length) { alert("❗ Прикрепите файл с контактами."); return false; }
    if (!brandInput.value.trim()) { alert("❗ Введите бренд."); return false; }
    if (!periodInput.value.trim()) { alert("❗ Введите период."); return false; }
    if (!templateInput.value.trim()) { alert("❗ Введите шаблон письма."); return false; }
    return true;
  }

  if (delayedBtn) {
    delayedBtn.addEventListener("click", () => {
      if (!validateForm()) return;
      secondsLeft = 5;
      countdownText.textContent = `Отправка через ${secondsLeft} сек...`;
      countdownBox.style.display = "block";
      delayedBtn.disabled = true;

      countdownTimer = setInterval(() => {
        secondsLeft -= 1;
        if (secondsLeft <= 0) {
          clearInterval(countdownTimer);
          countdownBox.style.display = "none";
          delayedBtn.disabled = false;
          if (window.htmx) window.htmx.trigger(form, "submitForm");
        } else {
          countdownText.textContent = `Отправка через ${secondsLeft} сек...`;
        }
      }, 1000);
    });
  }

  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      clearInterval(countdownTimer);
      countdownBox.style.display = "none";
      delayedBtn.disabled = false;
    });
  }
}

// Persist last inputs to datalist
function initDatalistPersistence() {
  const fields = [
    { name: 'brand', listId: 'brand-list' },
    { name: 'period', listId: 'period-list' },
    { name: 'cc_list', listId: 'cc-list' },
    { name: 'doc', listId: 'doc-list' }
  ];

  fields.forEach(({ name, listId }) => {
    const input = document.querySelector(`input[name="${name}"]`);
    const datalist = document.getElementById(listId);
    if (!input || !datalist) return;

    const saved = JSON.parse(localStorage.getItem(name) || '[]');
    saved.slice(-10).forEach(val => {
      const option = document.createElement('option');
      option.value = val;
      datalist.appendChild(option);
    });

    input.addEventListener('blur', () => {
      const value = input.value.trim();
      if (!value) return;
      let values = JSON.parse(localStorage.getItem(name) || '[]');
      if (!values.includes(value)) {
        values.push(value);
        localStorage.setItem(name, JSON.stringify(values.slice(-10)));
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initTemplates();
  initSubjectPreview();
  initPreviewHandlers();
  initDelayedSubmit();
  initDatalistPersistence();
});