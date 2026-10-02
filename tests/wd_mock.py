"""A stand-in for a Workday career site, for tests. No internet.

The page structure and every data-automation-id are copied from real Workday application pages (captured by the bot's
probe runs on 2026-10-01), and the behaviour copies what makes the real thing hard to automate:

  * the job page, 'Start Your Application', then the application flow with its progress bar;
  * account creation, 'verify your email', sign-in, 'forgot password';
  * choices that open in a pop-up outside the form ('How Did You Hear About Us?' with groups and sub-lists);
  * 'already selected' chips that look like choices to a careless script;
  * the name and address boxes being redrawn (new elements) when the State is picked;
  * radio / checkbox boxes that only react to a click on their label;
  * Work Experience / Education blocks that only exist after 'Add';
  * Next that sends the page back with complaints when something required is missing.

Tenants (first part of the path) switch behaviours on:
  acme       plain flow; the new account is signed in straight away
  verifyco   the new account must be verified from an emailed link before it can sign in
  resetco    an account already exists with another password (use ?seed=1 once): 'forgot password' is needed
  emailfirst the sign-in page first shows 'Sign in with email'
  brock      as one real employer did on 2026-10-01: the 'Sign in with Google / Apple / email' page comes back after every
             account step, the new account cannot sign in until its emailed link is opened (the refusal only says
             'Invalid Username/Password'), and that link leads back to the application, still signed out. Its résumé
             box is headed 'Resume/Cover Letter', as on the real pages in tests/fixtures/workday
  strict     Work Experience and Education are required (their headings carry a *)
  strict2    the same, but Workday only says so after Next
  strict3    like strict, and the employer's school list does not have your school (it has look-alikes and 'Other')
  bounce     Application Questions is sent back once by the server with a banner and no field marked
  closed     the posting is gone
  applied    the job page says you already applied
  nophone    the phone country code is not preselected
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.parse

APP_HTML = r"""<!doctype html><html lang="en-US"><head><meta charset="utf-8"><title>Mock Workday</title>
<style>
 body{font-family:Arial,sans-serif;margin:0;font-size:14px} header{padding:8px 16px;border-bottom:1px solid #ccc}
 main{padding:12px 24px;max-width:760px} label{display:block;margin-top:10px} input[type=text],input[type=password],textarea{width:320px}
 .hid{position:absolute;opacity:0;width:1px;height:1px} .valhold{display:none}
 [data-automation-id="activeListContainer"],ul.dd{position:absolute;background:#fff;border:1px solid #666;z-index:20;min-width:240px;max-height:260px;overflow:auto;margin:0;padding:0;list-style:none}
 .veil{position:fixed;inset:0;z-index:10} [role=option]{padding:5px 9px;cursor:pointer} [aria-invalid="true"]{outline:2px solid #c00}
 .err{color:#b00;font-size:12px} ol[data-automation-id="progressBar"]{display:flex;gap:14px;list-style:none;padding:0;font-size:11px}
 ol li label{display:inline;margin:0 3px 0 0} [data-automation-id="pageFooter"]{margin:24px 0} .pill{display:inline-block;border:1px solid #999;border-radius:9px;padding:0 6px;margin:2px}
 ul[data-automation-id="selectedItemList"]{list-style:none;padding:0;margin:2px 0} [data-automation-id="selectedItem"] p{display:inline;margin:0}
</style></head><body><div id="root"></div><script>
const parts = location.pathname.split('/').filter(Boolean);
const T = parts[0] || 'acme';
const K = k => 'wd:' + T + ':' + k;
const G = (k, d) => { const v = localStorage.getItem(K(k)); return v === null ? d : JSON.parse(v); };
const P = (k, v) => localStorage.setItem(K(k), JSON.stringify(v));
const base = '/' + parts.slice(0, 4).join('/');
// the application's own progress is kept per job; the account and what you typed before are kept per employer, as on Workday
const KJ = k => 'wd:' + T + ':' + (parts[3] || '') + ':' + k;
const GJ = (k, d) => { const v = localStorage.getItem(KJ(k)); return v === null ? d : JSON.parse(v); };
const PJ = (k, v) => localStorage.setItem(KJ(k), JSON.stringify(v));
const root = document.getElementById('root');
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $ = id => document.getElementById(id);
const mail = (kind, link) => fetch('/outbox?' + new URLSearchParams({tenant: T, kind, link}));
if (location.search.includes('seed=1') && !G('acct')) P('acct', {email: 'delgado@alumni.usc.edu', pw: 'Some-Old-Pw-9!', verified: true});
if (location.search.includes('reset=1')) { localStorage.clear(); }

const STEPS = ['Create Account/Sign In', 'My Information', 'My Experience', 'Application Questions', 'Voluntary Disclosures', 'Self Identify', 'Review'];
const PAGES = {1: 'applyFlowMyInfoPage', 2: 'applyFlowMyExpPage', 3: 'applyFlowPrimaryQuestionsPage', 4: 'applyFlowVoluntaryDisclosuresPage',
               5: 'applyFlowSelfIdentifyPage', 6: 'applyFlowReviewPage'};
const STATES = ['Alabama', 'Alaska', 'Arizona', 'California', 'Colorado', 'New York', 'Texas'];
const TREES = {
  source: {'Job Board': ['Indeed.com', 'LinkedIn', 'Glassdoor'], 'Social Media': ['Facebook', 'Instagram'], 'Referral': ['Employee Referral'], 'Career Fair': null},
  countryPhoneCode: ['United States of America (+1)', 'Canada (+1)', 'Mexico (+52)'],
  skills: ['Excel', 'SQL', 'Python', 'Project Management', 'Process Improvement'],
  school: T === 'strict3' ? ['Colorado School of Mines', 'University of California', 'Southern California Institute of Architecture', 'California Southern University', 'Other']
                          : ['Colorado School of Mines', 'University of Colorado Boulder', 'University of Southern California', 'Other'],
  fieldOfStudy: ['Business Administration', 'Industrial Engineering', 'Mechanical Engineering', 'Other'],
};
const MULTI = {skills: true};
let data = G('data', Object.assign({'country--country': 'United States of America'}, T === 'nophone' ? {} : {'phoneNumber--countryPhoneCode': ['United States of America (+1)']}));
let bounced = GJ('bounced', false);
const save = () => P('data', data);
let uid = 0; const rid = () => 'q' + Math.random().toString(36).slice(2, 6) + (uid++);

// ---------------------------------------------------------------- widgets
const star = req => req ? '<abbr title="required">*</abbr>' : '';
const text = (id, key, label, req, type) => `<div data-automation-id="formField-${key}"><label for="${id}">${label}${star(req)}</label>` +
  `<input id="${id}" type="${type || 'text'}" name="${key}" aria-required="${!!req}" value="${esc(data[id])}"></div>`;
const area = (id, key, q, req) => `<div data-automation-id="formField-${key}"><fieldset><legend><div id="label-${key}"><div data-automation-id="richText"><b>${q}</b>${star(req)}</div></div></legend>` +
  `<textarea id="${id}" aria-required="${!!req}">${esc(data[id])}</textarea></fieldset></div>`;
const dd = (id, key, label, req, opts, q) => `<div data-automation-id="formField-${key}">` +
  (q ? `<fieldset><legend><div id="rich-${key}"><div data-automation-id="richText"><b>${q}</b>${star(req)}</div></div></legend>` : `<label for="${id}">${label}${star(req)}</label>`) +
  `<button id="${id}" type="button" name="${key}" aria-haspopup="listbox" aria-label="${esc(label)} ${esc(data[id] || 'Select One')} ${req ? 'Required' : ''}" ` +
  `value="${data[id] ? 'x1' : ''}" data-opts="${esc(JSON.stringify(opts))}">${esc(data[id] || 'Select One')}</button><input class="valhold" type="text" value="${data[id] ? 'x1' : ''}">` +
  (q ? '</fieldset>' : '') + '</div>';
const pills = sel => `<div data-automation-id="promptAriaInstruction">${sel.length} item${sel.length === 1 ? '' : 's'} selected${sel.length ? ', ' + esc(sel.join(', ')) : ''}</div>` +
  `<ul data-automation-id="selectedItemList" role="listbox" aria-label="items selected">` + sel.map(s =>
    `<li data-automation-id="menuItem" role="presentation"><div class="pill" data-automation-id="selectedItem" role="option" aria-label="${esc(s)}, press delete to clear value.">` +
    `<div data-automation-id="DELETE_charm" role="presentation"></div><p data-automation-id="promptOption" data-automation-label="${esc(s)}">${esc(s)}</p></div></li>`).join('') + '</ul>';
const prompt = (id, key, label, req, tree) => `<div data-automation-id="formField-${key}"><label for="${id}">${label}${star(req)}</label>` +
  `<div data-automation-id="multiSelectContainer" data-uxi-widget-type="multiselect"><div data-automation-id="multiselectInputContainer">` +
  `<input id="${id}" aria-required="${!!req}" aria-invalid="false" placeholder="Search" data-uxi-widget-type="selectinput" data-tree="${tree}">` +
  `<span class="pillbox">${pills(data[id] || [])}</span></div><span data-automation-id="promptIcon" aria-invalid="false"></span></div></div>`;
const yesno = (key, q, req) => { const a = rid(), b = rid(); return `<div data-automation-id="formField-${key}"><fieldset><legend><label id="rl-${key}">${q}${star(req)}</label></legend>` +
  `<div id="grp-${key}" name="${key}" aria-required="${!!req}" aria-labelledby="rl-${key}">` +
  `<div><input class="hid" id="${a}" type="radio" name="${key}" value="true" ${data[key] === 'true' ? 'checked' : ''} aria-checked="${data[key] === 'true'}"><label for="${a}">Yes</label></div>` +
  `<div><input class="hid" id="${b}" type="radio" name="${key}" value="false" ${data[key] === 'false' ? 'checked' : ''} aria-checked="${data[key] === 'false'}"><label for="${b}">No</label></div>` +
  `</div></fieldset></div>`; };
const check = (id, key, label, req, aid) => `<div data-automation-id="formField-${key}"><label for="${id}">${label}${star(req)}</label>` +
  `<input class="hid" id="${id}" type="checkbox" name="${key}" ${aid ? `data-automation-id="${aid}"` : ''} aria-required="${!!req}" ${data[id] ? 'checked' : ''} aria-checked="${!!data[id]}"></div>`;
const date = (key, label, req, withDay) => { const v = data[key] || {}; return `<div data-automation-id="formField-${key}"><fieldset><legend>${label}${star(req)}</legend>` +
  `<div data-automation-id="dateInputWrapper">` +
  `<input data-automation-id="dateSectionMonth-input" id="${key}-dateSectionMonth-input" role="spinbutton" aria-label="Month" placeholder="MM" maxlength="2" size="2" value="${esc(v.m)}">/` +
  (withDay ? `<input data-automation-id="dateSectionDay-input" id="${key}-dateSectionDay-input" role="spinbutton" aria-label="Day" placeholder="DD" maxlength="2" size="2" value="${esc(v.d)}">/` : '') +
  `<input data-automation-id="dateSectionYear-input" id="${key}-dateSectionYear-input" role="spinbutton" aria-label="Year" placeholder="YYYY" maxlength="4" size="4" value="${esc(v.y)}">` +
  `</div></fieldset></div>`; };
const group = (name, inner) => `<div role="group" aria-labelledby="${name.replace(/ /g, '-')}-section"><h4 id="${name.replace(/ /g, '-')}-section">${name}</h4>${inner}</div><div data-automation-id="smartDivider"></div>`;

function upload() {
  const files = data.files || [];
  const head = T === 'brock' ? 'Resume/Cover Letter' : 'Resume/CV';
  return `<div role="group" aria-labelledby="Resume/CV-section"><h4 id="Resume/CV-section">${head}</h4><div data-automation-id="formField-"><label id="label5">Upload a file (5MB max)${star(true)}</label>` +
    `<div data-automation-id="attachments-FileUpload" aria-labelledby="label5"><div data-automation-id="file-upload-drop-zone">Drop files here or ` +
    `<button data-automation-id="select-files" id="resumeAttachments--attachments" type="button">Select files</button></div>` +
    `<input data-automation-id="file-upload-input-ref" type="file" class="hid">` + files.map(n =>
      `<div data-automation-id="file-upload-item"><div data-automation-id="ariaLive"><div data-automation-id="ariaLiveMessage" role="alert" aria-live="polite">${esc(n)} successfully uploaded</div></div>` +
      `<div data-automation-id="file-upload-item-name">${esc(n)}</div><div data-automation-id="file-upload-successful">Successfully Uploaded!</div>` +
      `<button data-automation-id="delete-file" type="button" aria-label="Delete ${esc(n)}">x</button></div>`).join('') + `</div></div></div><div data-automation-id="smartDivider"></div>`;
}
function section(name, key, rows) {
  const req = T === 'strict' || T === 'strict3';
  return `<div role="group" aria-labelledby="${name.replace(/ /g, '-')}-section"><h4 id="${name.replace(/ /g, '-')}-section">${name}${req ? ' ' + star(true) : ''}</h4>${rows}` +
    `<div><button data-automation-id="add-button" data-sec="${key}" type="button">Add${rows ? ' Another' : ''}</button></div></div><div data-automation-id="smartDivider"></div>`;
}
const workRow = n => { const p = 'workExperience-' + n; const cur = !!data[p + '--currentlyWorkHere'];
  return `<div data-automation-id="${p}"><h5>Work Experience ${n}</h5>` + text(p + '--jobTitle', 'jobTitle', 'Job Title', true) + text(p + '--companyName', 'companyName', 'Company', true) +
    text(p + '--location', 'location', 'Location', false) + check(p + '--currentlyWorkHere', 'currentlyWorkHere', 'I currently work here', false) +
    date(p + '--startDate', 'From', true, false) + (cur ? '' : date(p + '--endDate', 'To', true, false)) +
    `<div data-automation-id="formField-roleDescription"><label for="${p}--roleDescription">Role Description</label><textarea id="${p}--roleDescription">${esc(data[p + '--roleDescription'])}</textarea></div></div>`; };
const eduRow = n => { const p = 'education-' + n;
  return `<div data-automation-id="${p}"><h5>Education ${n}</h5>` + prompt(p + '--school', 'schoolName', 'School or University', true, 'school') +
    dd(p + '--degree', 'degree', 'Degree', true, ["Associate's Degree", "Bachelor's Degree", "Master's Degree", 'Doctorate']) +
    prompt(p + '--fieldOfStudy', 'fieldOfStudy', 'Field of Study', false, 'fieldOfStudy') + text(p + '--gradeAverage', 'gradeAverage', 'Overall Result (GPA)', false) + '</div>'; };

const STEP_HTML = {
  1: () => group('source', prompt('source--source', 'source', 'How Did You Hear About Us?', true, 'source')) +
       group('previousWorker', yesno('candidateIsPreviousWorker', 'Have you previously worked for Acme as an employee or contractor? If Yes, please answer the questions below.', true)) +
       group('country', dd('country--country', 'country', 'Country', true, ['United States of America', 'Canada', 'Mexico'])) +
       `<div id="nameaddr">${nameAddr()}</div>` +
       group('Email Address', text('emailAddress--emailAddress', 'emailAddress', 'Email', true)) +
       group('Phone', dd('phoneNumber--phoneType', 'phoneType', 'Phone Device Type', true, ['Landline', 'Mobile']) +
             prompt('phoneNumber--countryPhoneCode', 'countryPhoneCode', 'Country Phone Code', true, 'countryPhoneCode') +
             text('phoneNumber--phoneNumber', 'phoneNumber', 'Phone Number', true) + text('phoneNumber--extension', 'extension', 'Phone Extension', false) +
             `<div><input data-automation-id="phone-sms-opt-in" id="${rid()}" type="checkbox" aria-checked="false"> <a data-automation-id="phone-terms-and-condition-link" href="#">Terms and Conditions</a></div>`),
  2: () => section('Work Experience', 'work', (data.workRows || []).map(workRow).join('')) + section('Education', 'edu', (data.eduRows || []).map(eduRow).join('')) +
       group('Skills', prompt('skills--skills', 'skills', 'Type to Add Skills', false, 'skills')) + upload() +
       group('Social Network URLs', text('socialNetworkAccounts--linkedInAccount', 'linkedInAccount', 'LinkedIn URL', false)),
  3: () => `<div role="group" aria-labelledby="primaryQuestionnaire-section">` +
       dd('primaryQuestionnaire--q1', 'q1', '', true, ['Yes', 'No'], 'Are you legally able to work in the U.S. for any employer?') +
       dd('primaryQuestionnaire--q2', 'q2', '', true, ['Yes', 'No'], 'Will you now or in the future require visa sponsorship for employment at Acme?') +
       dd('primaryQuestionnaire--q3', 'q3', '', true, ['High School Diploma or GED', "Associate's Degree", "Bachelor's Degree", "Master's Degree"], 'What is your highest level of education attained?') +
       dd('primaryQuestionnaire--q4', 'q4', '', true, ['Yes', 'No'], 'Are you subject to any post-employment restrictions (e.g. non-compete or restrictive covenant agreement) that would prevent you from working at Acme?') +
       area('primaryQuestionnaire--q5', 'q5', 'What are your salary expectations?', true) + '</div>',
  4: () => group('Voluntary Disclosures', dd('personalInfoUS--gender', 'gender', 'Gender', false, ['Female', 'Male', 'I do not wish to answer']) +
       dd('personalInfoUS--veteranStatus', 'veteranStatus', 'Veteran Status', true, ['I am a protected veteran', 'I am not a protected veteran', 'I do not wish to self-identify'])) +
       group('Terms and Conditions', check('termsAndConditions--acceptTermsAndAgreements', 'acceptTermsAndAgreements', 'Yes, I have read and consent to the terms and conditions', true, 'agreementCheckbox')),
  5: () => group('Self Identify', text('selfIdentifiedDisabilityData--name', 'name', 'Name', true) + date('selfIdentifiedDisabilityData--dateSignedOn', 'Date', true, true) +
       `<div data-automation-id="formField-disabilityStatus"><fieldset><legend>Please check one of the boxes below:${star(true)}</legend>` +
       ['Yes, I have a disability, or have had one in the past', 'No, I do not have a disability and have not had one in the past', 'I do not want to answer'].map((t, i) => { const id = rid();
          return `<div><input class="hid" id="${id}" type="checkbox" name="disab-${i}" data-disab="${i}" ${data.disab === i ? 'checked' : ''} aria-checked="${data.disab === i}"><label for="${id}">${t}</label></div>`; }).join('') +
       '</fieldset></div>'),
  6: () => `<h4>Review</h4><p>Please review your application for ${esc(data['name--legalName--firstName'])}.</p>`,
};
function nameAddr() {
  return group('Legal Name', text('name--legalName--firstName', 'legalName--firstName', 'First Name', true) + text('name--legalName--lastName', 'legalName--lastName', 'Last Name', true) +
           check('name--preferredCheck', 'preferredCheck', 'I have a preferred name', false)) +
         group('Address', text('address--addressLine1', 'addressLine1', 'Address Line 1', true) + text('address--addressLine2', 'addressLine2', 'Address Line 2', false) +
           text('address--city', 'city', 'City', true) + dd('address--countryRegion', 'countryRegion', 'State', true, STATES) + text('address--postalCode', 'postalCode', 'Postal Code', true));
}

// ---------------------------------------------------------------- pop-ups
let pop = null, veil = null;
function closePop() { if (pop) pop.remove(); if (veil) veil.remove(); pop = veil = null; }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePop(); });
function place(el, anchor) { const r = anchor.getBoundingClientRect(); el.style.left = (r.left + scrollX) + 'px'; el.style.top = (r.bottom + scrollY + 2) + 'px'; }
function openList(btn) {
  closePop();
  veil = document.createElement('div'); veil.className = 'veil'; veil.onclick = closePop; document.body.appendChild(veil);
  pop = document.createElement('ul'); pop.className = 'dd'; pop.setAttribute('role', 'listbox');
  pop.innerHTML = ['Select One'].concat(JSON.parse(btn.dataset.opts)).map(o => `<li role="option"><div>${esc(o)}</div></li>`).join('');
  pop.querySelectorAll('li').forEach(li => li.onclick = () => { const v = li.innerText.trim(); data[btn.id] = v === 'Select One' ? '' : v; save(); closePop();
    btn.innerText = v; btn.value = v === 'Select One' ? '' : 'x1'; btn.removeAttribute('aria-invalid');
    if (btn.id === 'address--countryRegion') { keep(); $('nameaddr').innerHTML = nameAddr(); bind($('nameaddr')); }     // Workday redraws these boxes
  });
  document.body.appendChild(pop); place(pop, btn);
}
function openPrompt(input, query) {
  closePop();
  const tree = TREES[input.dataset.tree], flat = Array.isArray(tree);
  veil = document.createElement('div'); veil.className = 'veil'; veil.onclick = closePop; document.body.appendChild(veil);
  pop = document.createElement('div'); pop.setAttribute('data-automation-id', 'activeListContainer');
  const leaves = flat ? tree : Object.entries(tree).flatMap(([g, kids]) => kids === null ? [g] : kids);
  const pick = t => { const multi = MULTI[input.dataset.tree]; const cur = data[input.id] || [];
    data[input.id] = multi ? [...new Set(cur.concat([t]))] : [t]; save(); input.value = ''; input.setAttribute('aria-invalid', 'false');
    input.parentElement.querySelector('.pillbox').innerHTML = pills(data[input.id]); if (!multi) closePop(); };
  const show = (items, isLeaf) => { pop.innerHTML = '<div role="listbox">' + items.map(t =>
      `<div role="option" data-automation-id="menuItem" aria-label="${esc(t)}"><div data-automation-id="${isLeaf(t) ? 'promptLeafNode' : 'promptOption'}" data-automation-label="${esc(t)}">${esc(t)}</div></div>`).join('') +
      (items.length ? '' : '<div data-automation-id="noResults">No Items.</div>') + '</div>';
    pop.querySelectorAll('[role=option]').forEach(o => o.onclick = () => { const t = o.getAttribute('aria-label');
      if (flat || tree[t] === null || !(t in tree)) pick(t); else show(tree[t], () => true); }); };
  if (query) { const m = leaves.filter(t => t.toLowerCase().includes(query.toLowerCase())); if (m.length === 1 && m[0].toLowerCase() === query.toLowerCase()) { document.body.appendChild(pop); pick(m[0]); closePop(); return; } show(m, () => true); }
  else show(flat ? tree : Object.keys(tree), t => flat || tree[t] === null);
  document.body.appendChild(pop); place(pop, input);
}

// ---------------------------------------------------------------- binding and validation
function keep() { root.querySelectorAll('input[type=text], textarea').forEach(i => { if (i.id && !i.dataset.tree && !/dateSection/.test(i.id)) data[i.id] = i.value; }); save(); }
function bind(scope) {
  scope.querySelectorAll('button[aria-haspopup="listbox"]').forEach(b => b.onclick = () => openList(b));
  scope.querySelectorAll('input[data-tree]').forEach(i => { i.onclick = () => openPrompt(i); i.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); openPrompt(i, i.value); } }; });
  scope.querySelectorAll('input.hid[type=radio], input.hid[type=checkbox]').forEach(i => {
    const lab = scope.querySelector(`label[for="${i.id}"]`); let viaLabel = false;
    if (lab) lab.addEventListener('click', () => { viaLabel = true; setTimeout(() => { viaLabel = false; }, 50); }, true);
    i.addEventListener('click', e => { if (!viaLabel) { e.preventDefault(); return; }             // like Workday: only the label works
      setTimeout(() => { if (i.type === 'radio') { data[i.name] = i.value; scope.querySelectorAll(`input[name="${i.name}"]`).forEach(x => x.setAttribute('aria-checked', String(x.checked))); }
        else if (i.dataset.disab !== undefined) { keep(); data.disab = i.checked ? +i.dataset.disab : null; save(); render(); }
        else { data[i.id] = i.checked; i.setAttribute('aria-checked', String(i.checked)); if (/currentlyWorkHere/.test(i.id)) { keep(); render(); } }
        save(); }, 0); });
  });
  scope.querySelectorAll('[data-automation-id="dateInputWrapper"] input').forEach(i => i.oninput = () => {
    const key = i.id.replace(/-dateSection(Month|Day|Year)-input$/, ''), part = /Month/.test(i.id) ? 'm' : /Day/.test(i.id) ? 'd' : 'y';
    i.value = i.value.replace(/\D/g, ''); (data[key] = data[key] || {})[part] = i.value; save();
    if (i.value.length >= i.maxLength) { const nx = i.nextElementSibling; if (nx && nx.tagName === 'INPUT') nx.focus(); } });
  const sel = scope.querySelector('[data-automation-id="select-files"]'), fi = scope.querySelector('[data-automation-id="file-upload-input-ref"]');
  if (sel) sel.onclick = () => fi.click();
  if (fi) fi.onchange = () => { if (fi.files.length) { data.files = (data.files || []).concat([fi.files[0].name]); keep(); render(); } };
  scope.querySelectorAll('[data-automation-id="add-button"]').forEach(b => b.onclick = () => { keep(); const k = b.dataset.sec === 'work' ? 'workRows' : 'eduRows';
    data[k] = (data[k] || []).concat([(data[k] || []).length + 1]); save(); render(); });
}
function validate(step) {
  const bad = [];
  const need = (id, label) => { const el = $(id); if (!el) return; const v = el.tagName === 'BUTTON' ? (el.innerText.trim() === 'Select One' ? '' : el.innerText) : el.dataset.tree ? (data[id] || []).join('') : el.value;
    if (!String(v).trim()) { el.setAttribute('aria-invalid', 'true'); bad.push(`Error: The field ${label} is required and must have a value.`); } else el.removeAttribute('aria-invalid'); };
  const dateOk = (key, label, day) => { const v = data[key] || {}; const el = $(key + '-dateSectionMonth-input'); if (!el) return;
    if (!(+v.m >= 1 && +v.m <= 12 && /^\d{4}$/.test(v.y || '') && (!day || (+v.d >= 1 && +v.d <= 31)))) { el.setAttribute('aria-invalid', 'true'); bad.push(`Error: The field ${label} is required and must have a value.`); } else el.removeAttribute('aria-invalid'); };
  if (step === 1) {
    need('source--source', 'How Did You Hear About Us?');
    if (!data.candidateIsPreviousWorker) { $('grp-candidateIsPreviousWorker').setAttribute('aria-invalid', 'true'); bad.push('Error: Select an answer for the previous worker question.'); } else $('grp-candidateIsPreviousWorker').removeAttribute('aria-invalid');
    [['country--country', 'Country'], ['name--legalName--firstName', 'First Name'], ['name--legalName--lastName', 'Last Name'], ['address--addressLine1', 'Address Line 1'], ['address--city', 'City'],
     ['address--countryRegion', 'State'], ['address--postalCode', 'Postal Code'], ['emailAddress--emailAddress', 'Email'], ['phoneNumber--phoneType', 'Phone Device Type'],
     ['phoneNumber--countryPhoneCode', 'Country Phone Code'], ['phoneNumber--phoneNumber', 'Phone Number']].forEach(([i, l]) => need(i, l));
  }
  if (step === 2) {
    if (T === 'strict' || T === 'strict2' || T === 'strict3') {
      if (!(data.workRows || []).length) bad.push('Error: Work Experience is required. Add at least one entry.');
      if (!(data.eduRows || []).length) bad.push('Error: Education is required. Add at least one entry.');
    }
    (data.workRows || []).forEach(n => { const p = 'workExperience-' + n; need(p + '--jobTitle', 'Job Title'); need(p + '--companyName', 'Company'); dateOk(p + '--startDate', 'From');
      if (!data[p + '--currentlyWorkHere']) dateOk(p + '--endDate', 'To'); });
    (data.eduRows || []).forEach(n => { const p = 'education-' + n; need(p + '--school', 'School or University'); need(p + '--degree', 'Degree'); });
    if (!(data.files || []).length) bad.push('Error: Resume/CV is required.');
  }
  if (step === 3) { ['q1', 'q2', 'q3', 'q4', 'q5'].forEach((q, i) => need('primaryQuestionnaire--' + q, 'question ' + (i + 1)));
    if (T === 'bounce' && !bounced && !bad.length) { bounced = true; PJ('bounced', true); root.querySelectorAll('[aria-invalid]').forEach(x => x.removeAttribute('aria-invalid')); return ['Something went wrong. Please review your answers and try again.']; } }
  if (step === 4) { need('personalInfoUS--veteranStatus', 'Veteran Status');
    if (!data['termsAndConditions--acceptTermsAndAgreements']) { $('termsAndConditions--acceptTermsAndAgreements').setAttribute('aria-invalid', 'true'); bad.push('Error: You must accept the terms and conditions.'); } }
  if (step === 5) { need('selfIdentifiedDisabilityData--name', 'Name'); dateOk('selfIdentifiedDisabilityData--dateSignedOn', 'Date', true);
    if (data.disab === null || data.disab === undefined) bad.push('Error: Please check one of the boxes.'); }
  return bad;
}

// ---------------------------------------------------------------- pages
function progress(step) { return '<div aria-label="Application Progress"><ol data-automation-id="progressBar">' + STEPS.map((s, i) =>
  `<li data-automation-id="${i === step ? 'progressBarActiveStep' : i < step ? 'progressBarCompletedStep' : 'progressBarInactiveStep'}"><label aria-live="polite">${i === step ? 'current ' : ''}step ${i + 1} of ${STEPS.length}</label><label>${s}</label></li>`).join('') + '</ol></div>'; }
const chrome = inner => `<a data-automation-id="accessibilitySkipToMainContent" href="#mainContent" class="hid">Skip to main content</a>` +
  `<header data-automation-id="header"><button data-automation-id="utilityMenuButton" id="languageSelectorButton" aria-haspopup="listbox" aria-expanded="false">English</button> ` +
  `<button data-automation-id="utilityButtonSignIn">${G('signed') ? esc((G('acct') || {}).email) : 'Sign In'}</button></header><div id="mainContent"><main>${inner}</main></div>`;
function authPage() {
  const mode = G('authmode', T === 'brock' || (T === 'emailfirst' && !G('emailclicked')) ? 'social' : 'create'), acct = G('acct');
  let inner;
  if (mode === 'social') inner = `<h2 id="authViewTitle">Sign In</h2><button data-automation-id="GoogleSignInButton" type="button">Sign in with Google</button> OR <button data-automation-id="SignInWithEmailButton" type="button">Sign in with email</button>`;
  else if (mode === 'wait') inner = `<h2>Verify your account</h2><p>We sent a verification link. Please check your email to verify your account before signing in.</p>`;
  else if (mode === 'forgot') inner = `<h2>Forgot Password</h2><label for="fe">Email Address</label><input id="fe" type="text" data-automation-id="email"><button data-automation-id="resetPasswordSubmitButton" type="button">Reset Password</button>`;
  else if (mode === 'forgot-sent') inner = `<h2>Forgot Password</h2><p>If an account exists, an email with a link to reset your password has been sent.</p>`;
  else if (mode === 'signin') inner = `<h2 id="authViewTitle">Sign In</h2><label for="se">Email Address${star(true)}</label><input id="se" type="text" data-automation-id="email">` +
    `<label for="sp">Password${star(true)}</label><input id="sp" type="password" data-automation-id="password"><div id="autherr" class="err" role="alert"></div>` +
    `<div><button data-automation-id="signInSubmitButton" type="button">Sign In</button></div><button data-automation-id="forgotPasswordLink" role="link" type="button">Forgot your password?</button> ` +
    `<button data-automation-id="createAccountLink" role="link" type="button">Create Account</button>`;
  else inner = `<h2 id="authViewTitle">Create Account</h2><label for="ce">Email Address${star(true)}</label><input id="ce" type="text" data-automation-id="email">` +
    `<label for="cp">Password${star(true)}</label><input id="cp" type="password" data-automation-id="password"><label for="cv">Verify New Password${star(true)}</label><input id="cv" type="password" data-automation-id="verifyPassword">` +
    `<div><input id="cc" type="checkbox" data-automation-id="createAccountCheckbox"><label for="cc" style="display:inline">I agree to the terms</label></div><div id="autherr" class="err" role="alert"></div>` +
    `<div><button data-automation-id="createAccountSubmitButton" type="button">Create Account</button></div>Already have an account? <button data-automation-id="signInLink" role="link" type="button">Sign In</button>`;
  root.innerHTML = chrome(`<div data-automation-id="applyFlowPage"><h2 data-automation-id="jobTitleHeading">Operations Analyst</h2>${progress(0)}<div data-automation-id="signInContent">${inner}</div></div>`);
  const on = (aid, fn) => { const b = root.querySelector(`[data-automation-id="${aid}"]`); if (b) b.onclick = fn; };
  const go = m => { P('authmode', m); authPage(); };
  on('SignInWithEmailButton', () => { P('emailclicked', true); go('signin'); });
  on('signInLink', () => go('signin')); on('createAccountLink', () => go('create')); on('forgotPasswordLink', () => go('forgot'));
  on('createAccountSubmitButton', () => { const e = $('ce').value, p = $('cp').value;
    if (acct && acct.email === e) { $('autherr').innerText = 'ERROR: An account already exists for this email address.'; return; }
    if (!e || p !== $('cv').value || !$('cc').checked) { $('autherr').innerText = 'ERROR: Enter your email, matching passwords, and accept the terms.'; return; }
    if (!(p.length >= 8 && /[A-Z]/.test(p) && /[a-z]/.test(p) && /\d/.test(p) && /[^A-Za-z0-9]/.test(p))) { $('autherr').innerText = 'ERROR: Password must include an uppercase character, a numeric character and a special character.'; return; }
    if (T === 'verifyco') { P('acct', {email: e, pw: p, verified: false}); mail('verify', location.origin + '/' + T + '/verify'); go('wait'); }
    else if (T === 'brock') { P('acct', {email: e, pw: p, verified: false});
      mail('verify', location.origin + '/' + T + '/verify?redirect=' + encodeURIComponent(base + '/apply/applyManually')); go('social'); }
    else { P('acct', {email: e, pw: p, verified: true}); P('signed', true); PJ('step', 1); P('authmode', 'signin'); render(); } });
  on('signInSubmitButton', () => { const e = $('se').value, p = $('sp').value;
    if (acct && acct.email === e && acct.pw === p && !acct.verified && T === 'brock') { $('autherr').innerText = 'ERROR: Invalid Username/Password. Your account may be locked after too many incorrect attempts.'; return; }
    if (acct && acct.email === e && acct.pw === p && !acct.verified) { $('autherr').innerText = 'Your account has not been verified. Verify your email before signing in.'; mail('verify', location.origin + '/' + T + '/verify'); return; }
    if (!acct || acct.email !== e || acct.pw !== p) { $('autherr').innerText = 'ERROR: Invalid Username/Password. Your account may be locked after too many incorrect attempts.'; return; }
    P('signed', true); if (!GJ('step')) PJ('step', 1); render(); });
  on('resetPasswordSubmitButton', () => { mail('reset', location.origin + '/' + T + '/passwordreset'); go('forgot-sent'); });
}
function render() {
  closePop();
  if (T === 'closed') { root.innerHTML = chrome(`<div data-automation-id="errorContainer"><span data-automation-id="errorMessage">The page you are looking for doesn't exist.</span><button data-automation-id="searchForJobsButton">Search for Jobs</button></div>`); return; }
  if (parts[1] === 'verify') { const a = G('acct'); if (a) { a.verified = true; P('acct', a); } P('authmode', T === 'brock' ? 'social' : 'signin');
    const back = new URLSearchParams(location.search).get('redirect');
    if (back) { location.replace(back); return; }                       // Workday's link leads on to the application (signed out)
    root.innerHTML = '<p>Thank you. Your email address has been verified.</p>'; return; }
  if (parts[1] === 'passwordreset') { root.innerHTML = chrome(`<h2>Reset Password</h2><label for="np">New Password</label><input id="np" type="password" data-automation-id="password">` +
      `<label for="nv">Verify New Password</label><input id="nv" type="password" data-automation-id="verifyPassword"><div id="rmsg" class="err" role="alert"></div><button data-automation-id="resetPasswordSubmitButton" type="button">Change Password</button>`);
    root.querySelector('[data-automation-id="resetPasswordSubmitButton"]').onclick = () => { if ($('np').value && $('np').value === $('nv').value) { const a = G('acct') || {email: 'delgado@alumni.usc.edu'}; a.pw = $('np').value; a.verified = true; P('acct', a); P('authmode', 'signin');
      root.innerHTML = chrome('<p>Your password has been changed.</p>'); } else $('rmsg').innerText = 'ERROR: The passwords do not match.'; };
    return; }
  if (GJ('submitted')) { root.innerHTML = chrome(`<div data-automation-id="applyFlowPage"><h2>Application Submitted</h2><p>Congratulations! Your application has been submitted.</p></div>`); return; }
  if (parts[4] !== 'apply') {          // the job posting
    root.innerHTML = chrome(`<div data-automation-id="jobPostingPage"><h2 data-automation-id="jobPostingHeader">Operations Analyst</h2>` +
      (T === 'applied' ? `<div data-automation-id="alreadyApplied">You applied for this job on 09/30/2026.</div>` : `<a data-automation-id="adventureButton" role="button" href="${base}/apply">Apply</a>`) +
      `<div data-automation-id="jobPostingDescription"><p>Entry-level operations analyst. You will drive process improvement using Excel and SQL and coordinate vendor work. 0-2 years of experience. Bachelor's degree.</p>` +
      `<p>What you will do: keep the weekly operations dashboard current, document standard operating procedures, track vendor deliveries and invoices, and prepare short reports for the operations manager. ` +
      `What we look for: clear writing, comfort with spreadsheets, and care with details. This is a full-time salaried role with benefits. Pay range: $66,000 - $78,000 per year.</p></div></div>`);
    return; }
  if (parts[5] !== 'applyManually' && !G('signed')) {          // Start Your Application
    root.innerHTML = chrome(`<div data-automation-id="applyAdventurePage"><h2>Start Your Application</h2><h3>Operations Analyst</h3>` +
      `<a data-automation-id="autofillWithResume" role="button" href="${base}/apply/autofillWithResume">Autofill with Resume</a> ` +
      `<a data-automation-id="applyManually" role="button" href="${base}/apply/applyManually">Apply Manually</a> ` +
      `<a data-automation-id="useMyLastApplication" role="button" href="${base}/apply/useMyLastApplication">Use My Last Application</a></div>`);
    return; }
  if (!G('signed')) { authPage(); return; }
  const step = GJ('step', 1);
  root.innerHTML = chrome(`<div data-automation-id="applyFlowPage"><button data-automation-id="backToJobPosting" role="link" type="button">Back to Job Posting</button>` +
    `<h2 data-automation-id="jobTitleHeading">Operations Analyst</h2>${progress(step)}<h3>${STEPS[step]} <abbr>*</abbr> Indicates a required field</h3><div id="banner"></div>` +
    `<div data-automation-id="${PAGES[step]}">${STEP_HTML[step]()}</div>` +
    `<div data-automation-id="pageFooter">${step > 1 ? '<button data-automation-id="pageFooterBackButton">Back</button> ' : ''}<button data-automation-id="pageFooterNextButton">${step === 6 ? 'Submit' : step === 1 ? 'Next' : 'Save and Continue'}</button></div></div>`);
  bind(root);
  root.querySelector('[data-automation-id="pageFooterNextButton"]').onclick = () => {
    keep();
    if (step === 6) { PJ('submitted', true); fetch('/submitted?' + new URLSearchParams({tenant: T, job: parts[3] || '', data: JSON.stringify(data)})).then(render, render); return; }
    setTimeout(() => {                                   // the server round trip
      const bad = validate(step);
      if (bad.length) { $('banner').innerHTML = `<button data-automation-id="errorBanner" type="button">Errors Found: ${bad.length}</button>` + bad.map(b => `<div class="err" data-automation-id="errorMessage" role="alert">${esc(b)}</div>`).join(''); return; }
      PJ('step', step + 1); render(); scrollTo(0, 0); }, 350);
  };
  const back = root.querySelector('[data-automation-id="pageFooterBackButton"]'); if (back) back.onclick = () => { keep(); PJ('step', step - 1); render(); };
}
render();
</script></body></html>"""


class State:
    def __init__(self):
        self.outbox: list[dict] = []          # emails the mock 'sent': {'tenant', 'kind', 'link'}
        self.submitted: list[dict] = []       # {'tenant', 'data'}


def serve() -> tuple:
    """Start the mock. Returns (server, base_url, State)."""
    st = State()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path == "/outbox":
                st.outbox.append(q)
                body, ctype = b"ok", "text/plain"
            elif u.path == "/submitted":
                st.submitted.append({"tenant": q.get("tenant"), "job": q.get("job"), "data": json.loads(q.get("data") or "{}")})
                body, ctype = b"ok", "text/plain"
            elif u.path == "/favicon.ico":
                body, ctype = b"", "image/x-icon"
            else:
                body, ctype = APP_HTML.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", st


def job_url(base: str, tenant: str, req: str = "R-100") -> str:
    return f"{base}/{tenant}/job/Denver-CO/Operations-Analyst_{req}"
