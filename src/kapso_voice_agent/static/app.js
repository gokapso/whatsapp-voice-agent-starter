'use strict';
// Operator console. The console access key (OPERATOR_TOKEN) is kept in sessionStorage for this
// tab only and sent as a bearer header; the page itself holds no secret.
const $ = selector => document.querySelector(selector);
const STORAGE = 'consoleAccessKey';
const clock = new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit'});
const plural = new Intl.PluralRules('en');
const KEY_HELP = 'Copy it again with uv run voice-agent console-key, then paste it here.';
const UNREACHABLE = 'Unable to reach the server. The console tries again every 2 seconds.';
let key = sessionStorage.getItem(STORAGE) || '';
let peer, microphone, browserRef, outboundRef, allowedRecipient, state, poller, timer, callStartedAt;
let attempt = 0;  // bumped by sign-out, so a call that is still starting knows to hang itself up

class Unauthorized extends Error {}

async function api(path, body, withKey = key) {
  const options = {headers: {Authorization: `Bearer ${withKey}`}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`/operator/api${path}`, options);
  } catch {
    throw new Error('Unable to reach the server. Check that it is running, then try again.');
  }
  if (response.status === 401) { throw new Unauthorized(); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { throw new Error(data.detail || `The server answered with error ${response.status}.`); }
  return data;
}

// Text with inline commands. Parts are strings or {code: '...'}; nothing is parsed as HTML.
function fill(element, ...parts) {
  element.replaceChildren(...parts.map(part => {
    if (typeof part === 'string') { return part; }
    const code = document.createElement('code');
    code.textContent = part.code;
    return code;
  }));
}

const ICONS = {
  ok: '<path d="M20 6 9 17l-5-5"/>',
  todo: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5m0 3v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  live: '<circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="9"/>',
};

function icon(kind, className = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `${className} ${kind}`);
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[kind];
  return svg;
}

// Sign in and out --------------------------------------------------------------------------

function showSignedIn(signedIn) {
  $('#sign-in').hidden = signedIn;
  $('#workspace').hidden = !signedIn;
  $('#session').hidden = !signedIn;
}

function showKeyError(message) {
  $('#key-error').textContent = message;
  $('#key').setAttribute('aria-invalid', message ? 'true' : 'false');
}

function forgetKey() {
  key = '';
  sessionStorage.removeItem(STORAGE);
  clearInterval(poller);
  poller = undefined;
}

async function signIn(candidate, restoring = false) {
  const button = $('#sign-in-button');
  button.disabled = true;
  button.textContent = 'Signing in…';
  showKeyError('');
  try {
    state = await api('/state', undefined, candidate);
  } catch (error) {
    forgetKey();
    showSignedIn(false);
    showKeyError(error instanceof Unauthorized
      ? (restoring ? `The saved key no longer works. The server may have a new key. ${KEY_HELP}`
        : `This key does not match this server. ${KEY_HELP}`)
      : error.message);
    $('#key').focus();
    return;
  } finally {
    button.disabled = false;
    button.textContent = 'Sign in';
  }
  key = candidate;
  sessionStorage.setItem(STORAGE, key);
  $('#key').value = '';
  showSignedIn(true);
  render();
  poller = setInterval(refresh, 2000);
  if (!restoring) { $('#readiness-title').focus(); }
}

$('#sign-in-form').onsubmit = event => {
  event.preventDefault();
  const candidate = $('#key').value.trim();
  if (!candidate) {
    showKeyError('Enter the console access key.');
    $('#key').focus();
    return;
  }
  signIn(candidate);
};

async function signOut(message = '') {
  attempt += 1;
  if (browserRef || peer) { await hangUp(); }
  if (outboundRef) {
    // Signing out ends the console's WhatsApp call too; its controls disappear with the key.
    await api(`/calls/${encodeURIComponent(outboundRef)}/hangup`, {}).catch(() => {});
    outboundRef = undefined;
    $('#outbound-stop').disabled = true;
    $('#outbound-status').textContent = '';
  }
  forgetKey();
  showSignedIn(false);
  showKeyError(message);
  $('#key').focus();
}

$('#sign-out').onclick = () => signOut();

// What the server can do, and the one thing to do next ------------------------------------

function vendor(voice) {
  return voice.provider === 'grok' ? 'xAI' : 'ElevenLabs';
}

function voiceName(voice) {
  if (voice.offline_fake_agent) { return `Offline stand-in (${voice.provider === 'grok' ? 'Grok' : 'ElevenLabs'} protocol)`; }
  return voice.provider === 'grok' ? `Grok Voice · ${voice.model} · voice ${voice.voice}` : 'ElevenLabs Agents';
}

const PROBLEMS = {
  auth: voice => `${vendor(voice)} rejected the API key. Check ${voice.provider === 'grok' ? 'XAI_API_KEY' : 'ELEVENLABS_API_KEY'} in the .env file of the server, then restart the server.`,
  billing: voice => `${vendor(voice)} refused the session: the account is out of credits or over its spending limit. Add credits or raise the limit in the ${vendor(voice)} billing settings, then try again.`,
  rate_limited: voice => `${vendor(voice)} is limiting requests for this key. Wait one minute, then start the call again.`,
  not_found: voice => (voice.provider === 'grok'
    ? `xAI did not open ${voice.model}. Check grok.model in agent/agent.toml, and that your xAI team can use Grok Voice.`
    : 'ElevenLabs did not find the agent. Check ELEVENLABS_AGENT_ID, or run uv run voice-agent agent apply.'),
  unreachable: voice => `Unable to reach ${vendor(voice)} from the server. Check its internet connection, then try again.`,
  failed: voice => `${vendor(voice)} ended the session before it was ready. Open the technical event log for details, then try again.`,
};

// KAPSO_REGISTER_WEBHOOK=1: why the server could not make itself the number's webhook.
const WEBHOOK_PROBLEMS = {
  other: ['Your number sends its webhook to another server. Remove that webhook in the Kapso dashboard, then restart this server.'],
  failed: ['Unable to register this server as the webhook of your number. Check ', {code: 'KAPSO_API_KEY'}, ' and ',
    {code: 'WHATSAPP_PHONE_NUMBER_ID'}, ', then restart this server.'],
  missing_settings: ['Unable to register the webhook: this server has no public domain. Add one in Railway, then restart this server.'],
};

// The newest WhatsApp call was refused by Kapso: the number's Inbox fallback (or a dashboard
// voice agent) took it, so this server could not answer.
function lastCallNotAnswerable() {
  const call = state.recent_calls.find(c => c.kind === 'whatsapp_inbound');
  return Boolean(call) && state.events.some(e => e.ref === call.ref && e.code === 'not_answerable');
}

const NOT_ANSWERABLE = ['Kapso gave the last call to the Inbox, so this server could not answer it. Turn off Inbox '
  + 'fallback for this number in Kapso, and remove any dashboard voice agent from it. Then call again.'];

function connectedWhatsAppCall() {
  return state.recent_calls.find(call => call.kind !== 'browser' && call.connected_at);
}

// The single next action. Blocking steps first, then the path to a real phone call.
function nextStep() {
  const voice = state.voice;
  const name = vendor(voice);
  if (voice.setup_step === 'api_key') {
    return {blocking: true, parts: [`Add your ${name} API key. Run `, {code: 'uv run voice-agent setup'}, ', then restart the server.']};
  }
  if (voice.setup_step === 'create_agent') {
    return {blocking: true, parts: ['Create the ElevenLabs agent. Run ', {code: 'uv run voice-agent agent apply --yes --save-agent-id'},
      ', then restart the server.']};
  }
  if (!state.calendar) {
    return {blocking: true, parts: ['Choose a calendar. Run ', {code: 'uv run voice-agent setup'},
      ' and pick Cal.com, or the local calendar for a first test.']};
  }
  if (!state.agent_ready) {
    return {blocking: true, parts: ['Create the local secrets. Run ', {code: 'uv run voice-agent setup'}, ', then restart the server.']};
  }
  if (state.recording.next_greeting_says_recorded === null && state.recording.last_check_error) {
    return {blocking: true, parts: ['Unable to read the recording setting of the ElevenLabs agent. Check ',
      {code: 'ELEVENLABS_API_KEY'}, ' and ', {code: 'ELEVENLABS_AGENT_ID'}, ', then restart the server.']};
  }
  if (state.whatsapp_missing.length) {
    return {blocking: false, parts: ['Next: after a good test call, host this server and connect your WhatsApp number. See ',
      {code: 'docs/deploy.md'}, '.']};
  }
  if (WEBHOOK_PROBLEMS[state.kapso_webhook]) {
    return {blocking: false, parts: WEBHOOK_PROBLEMS[state.kapso_webhook]};
  }
  if (lastCallNotAnswerable()) {
    return {blocking: false, parts: NOT_ANSWERABLE};
  }
  const call = connectedWhatsAppCall();
  if (!call) {
    return {blocking: false, parts: ['Next: call your WhatsApp number from a phone. Only a phone call proves the full path.']};
  }
  return {blocking: false, parts: [`A WhatsApp call connected at ${clock.format(new Date(call.connected_at))}.`]};
}

function renderReadiness() {
  const step = nextStep();
  const onCall = Boolean(peer);
  const kind = onCall ? 'live' : (step.blocking ? 'todo' : 'ok');
  $('#readiness-icon').setAttribute('class', `icon readiness-icon ${kind}`);
  $('#readiness-icon').innerHTML = ICONS[kind];
  $('#readiness-title').textContent = onCall ? 'On a test call'
    : (step.blocking ? 'Finish setup to start a test call' : 'Ready for a test call');
  fill($('#next-step'), ...step.parts);

  const voice = state.voice;
  if (voice.offline_fake_agent) {
    $('#call-cost').textContent = 'Offline stand-in: you hear a tone, then your own voice. No AI and no cost.';
  } else {
    const calendar = state.calendar === 'calcom' ? 'real bookings in your Cal.com calendar' : 'the local development calendar';
    $('#call-cost').textContent = `Starts a real ${voice.provider === 'grok' ? 'Grok Voice session' : 'ElevenLabs conversation'}`
      + ` that ${vendor(voice)} bills per minute. It uses ${calendar}. It does not test WhatsApp audio.`;
  }
  const problem = voice.problem;
  $('#provider-problem').hidden = !problem;
  $('#provider-problem').textContent = problem
    ? `Last attempt, ${clock.format(new Date(problem.at))}: ${(PROBLEMS[problem.problem] || PROBLEMS.failed)(voice)}` : '';
  if (!peer) { $('#start').disabled = step.blocking || state.busy; }
}

// ['A', 'B', 'C'] as `A`, `B` and `C`.
function codeList(names) {
  return names.flatMap((name, index) => [index === 0 ? '' : (index === names.length - 1 ? ' and ' : ', '), {code: name}])
    .filter(part => part !== '');
}

function setupRow(name, kind, value, detail) {
  const term = document.createElement('dt');
  term.textContent = name;
  const description = document.createElement('dd');
  const line = document.createElement('p');
  line.className = 'setup-value';
  line.append(icon(kind, 'icon status-icon'), value);
  description.append(line);
  if (detail) {
    const note = document.createElement('p');
    note.className = 'setup-detail';
    fill(note, ...(Array.isArray(detail) ? detail : [detail]));
    description.append(note);
  }
  const row = document.createElement('div');
  row.className = 'setup-row';
  row.append(term, description);
  return row;
}

function renderSetup() {
  const voice = state.voice;
  const rows = [];
  const voiceDetail = {api_key: `Needs the ${vendor(voice)} API key.`, create_agent: 'Needs the ElevenLabs agent.'}[voice.setup_step]
    || (voice.offline_fake_agent ? 'A tone and an echo for development. Not a conversation.' : '');
  rows.push(setupRow('Voice provider', voice.setup_step ? 'todo' : 'ok', voiceName(voice), voiceDetail));

  if (state.calendar === 'calcom') {
    rows.push(setupRow('Calendar', 'ok', 'Cal.com', 'Test bookings are real bookings.'));
  } else if (state.calendar === 'local') {
    rows.push(setupRow('Calendar', 'info', 'Local development calendar', 'Bookings stay on this server. Use Cal.com for real callers.'));
  } else {
    rows.push(setupRow('Calendar', 'todo', 'Not chosen', ['Add ', {code: 'CAL_API_KEY'}, ', or set ', {code: 'CALENDAR=local'}, ' for a first test.']));
  }

  const recorded = state.recording.next_greeting_says_recorded;
  rows.push(recorded === null
    ? setupRow('Recording notice', 'info', 'Read at the start of each call', 'No call starts until the recording setting of the provider is known.')
    : setupRow('Recording notice', recorded ? 'ok' : 'info', recorded ? 'Callers hear “This call is recorded.”' : 'No recording notice',
      recorded && voice.provider === 'grok' && !voice.offline_fake_agent ? 'xAI keeps API traffic for 30 days.' : ''));

  if (state.whatsapp_missing.length) {
    rows.push(setupRow('WhatsApp', 'info', 'Not connected', ['Needs ', ...codeList(state.whatsapp_missing), '.']));
  } else if (WEBHOOK_PROBLEMS[state.kapso_webhook]) {
    rows.push(setupRow('WhatsApp', 'todo', 'Webhook not connected', WEBHOOK_PROBLEMS[state.kapso_webhook]));
  } else if (lastCallNotAnswerable()) {
    rows.push(setupRow('WhatsApp', 'todo', 'Calls go to the Kapso Inbox', NOT_ANSWERABLE));
  } else {
    const call = connectedWhatsAppCall();
    const webhook = ['registered', 'present'].includes(state.kapso_webhook) ? 'The webhook of the number points to this server. ' : '';
    rows.push(setupRow('WhatsApp', call ? 'ok' : 'info', 'Configured through Kapso', call
      ? `A call connected at ${clock.format(new Date(call.connected_at))}.`
      : `${webhook}Not proven yet. Call the number from a phone.`));
  }

  rows.push(state.outbound_enabled
    ? setupRow('Outbound calls', 'info', 'On', 'The form below rings real phones.')
    : setupRow('Outbound calls', 'info', 'Off', ['Set ', {code: 'ENABLE_OUTBOUND=1'}, ' to call people from this console.']));
  $('#setup').replaceChildren(...rows);
}

// Recent calls: built by the server from its in-memory event log ----------------------------

const KINDS = {browser: 'Browser test', whatsapp_inbound: 'WhatsApp, incoming', whatsapp_outbound: 'WhatsApp, outgoing'};

function duration(from, to) {
  const seconds = Math.max(0, Math.floor((to - from) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function recentItem(call) {
  let kind, status;
  if (call.active) {
    kind = 'live';
    status = call.connected_at ? `In progress, ${duration(new Date(call.connected_at), Date.now())}` : 'Connecting';
  } else if (call.problem) {
    kind = 'todo';
    status = 'Problem. See the event log.';
  } else if (call.connected_at) {
    kind = 'ok';
    status = `Ended after ${duration(new Date(call.connected_at), new Date(call.last_at))}`;
  } else {
    kind = 'info';
    status = 'Audio did not connect';
  }
  const tools = !call.tools ? 'No tool calls' : (plural.select(call.tools) === 'one' ? '1 tool call' : `${call.tools} tool calls`);
  const media = {direct: 'direct audio', relay: 'relayed audio'}[call.media];

  const item = document.createElement('li');
  item.className = 'recent-item';
  const time = document.createElement('time');
  time.className = 'recent-time';
  time.dateTime = call.started_at;
  time.textContent = clock.format(new Date(call.started_at));
  const what = document.createElement('div');
  const title = document.createElement('p');
  title.className = 'recent-kind';
  title.textContent = KINDS[call.kind];
  const detail = document.createElement('p');
  detail.className = 'recent-detail';
  detail.textContent = media ? `${tools} · ${media}` : tools;
  what.append(title, detail);
  const result = document.createElement('p');
  result.className = 'recent-status';
  result.append(icon(kind, 'icon status-icon'), status);
  item.append(time, what, result);
  return item;
}

function renderRecent() {
  $('#recent').replaceChildren(...state.recent_calls.map(recentItem));
  $('#recent-empty').hidden = state.recent_calls.length > 0;
}

function renderOutbound() {
  $('#outbound').hidden = !state.outbound_enabled;
  if (outboundRef && !state.calls.some(call => call.ref === outboundRef)) {
    outboundRef = undefined;
    $('#outbound-stop').disabled = true;
    $('#outbound-status').textContent = 'Call ended.';
  }
}

function render() {
  renderReadiness();
  renderSetup();
  renderRecent();
  renderOutbound();
  const details = e => ['media', 'http_status', 'code'].filter(key => e[key]).map(key => ` ${key}=${e[key]}`).join('');
  $('#events').textContent = state.events.map(e => `${e.at.slice(11, 23)} ${e.event}${e.ref ? ' ' + e.ref : ''}${details(e)}`).join('\n');
}

async function refresh() {
  if (!key || document.hidden) { return; }
  try {
    state = await api('/state');
  } catch (error) {
    if (error instanceof Unauthorized) {
      await signOut(`The key no longer works. The server may have a new key. ${KEY_HELP}`);
    } else {
      $('#call-status').textContent = UNREACHABLE;
    }
    return;
  }
  if ($('#call-status').textContent === UNREACHABLE) { $('#call-status').textContent = ''; }
  render();
}

// Browser test call -----------------------------------------------------------------------

function showCallControls(onCall) {
  const hadFocus = document.activeElement;
  $('#start').hidden = onCall;
  $('#mute').hidden = $('#stop').hidden = !onCall;
  if (onCall && hadFocus === $('#start')) { $('#stop').focus(); }
  if (!onCall && [$('#stop'), $('#mute')].includes(hadFocus)) { $('#start').focus(); }
}

function setMuted(muted) {
  $('#mute').setAttribute('aria-pressed', String(muted));
  if (microphone) { microphone.getAudioTracks().forEach(track => { track.enabled = !muted; }); }
}

function release() {
  if (microphone) { microphone.getTracks().forEach(track => track.stop()); }
  if (peer) { peer.close(); }
  microphone = peer = undefined;
  clearInterval(timer);
  timer = undefined;
  $('#timer').textContent = '';
  setMuted(false);
  showCallControls(false);
}

function callEnded(message) {
  const length = callStartedAt ? duration(callStartedAt, Date.now()) : '';
  callStartedAt = undefined;
  release();
  browserRef = undefined;
  $('#call-status').textContent = message || (length ? `Call ended after ${length}.` : 'Call ended.');
  if (state) { renderReadiness(); }
  refresh();
}

function microphoneMessage(error) {
  if (error.name === 'NotAllowedError') {
    return 'The browser blocks the microphone. Allow it for this site in the browser settings, then start the call again.';
  }
  if (error.name === 'NotFoundError') { return 'No microphone found. Connect one, then start the call again.'; }
  return 'Unable to use the microphone. Check the browser settings, then try again.';
}

$('#start').onclick = async () => {
  const started = attempt, startKey = key;
  $('#start').disabled = true;
  $('#call-status').textContent = 'Asking for microphone access…';
  try {
    microphone = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
  } catch (error) {
    release();
    $('#start').disabled = false;
    $('#call-status').textContent = microphoneMessage(error);
    return;
  }
  $('#call-status').textContent = 'Connecting…';
  try {
    peer = new RTCPeerConnection();
    peer.ontrack = event => { $('#audio').srcObject = new MediaStream([event.track]); };
    peer.onconnectionstatechange = () => {
      if (!peer) { return; }
      if (peer.connectionState === 'connected' && !callStartedAt) {
        callStartedAt = Date.now();
        $('#call-status').textContent = 'On a call. Say hello.';
        timer = setInterval(() => { $('#timer').textContent = duration(callStartedAt, Date.now()); }, 1000);
        renderReadiness();
      } else if (['failed', 'closed', 'disconnected'].includes(peer.connectionState)) {
        callEnded(peer.connectionState === 'failed' && !callStartedAt
          ? 'The audio could not connect. Browser test calls work when the server runs on your computer. On Railway, call your WhatsApp number instead.' : '');
      }
    };
    microphone.getTracks().forEach(track => peer.addTrack(track, microphone));
    await peer.setLocalDescription(await peer.createOffer());
    await new Promise((resolve, reject) => {
      if (peer.iceGatheringState === 'complete') { resolve(); return; }
      const wait = setTimeout(() => reject(new Error('The browser took too long to prepare the audio connection. Try again.')), 10000);
      peer.onicegatheringstatechange = () => {
        if (peer && peer.iceGatheringState === 'complete') { clearTimeout(wait); resolve(); }
      };
    });
    const answer = await api('/browser-call', {sdp: peer.localDescription.sdp});
    if (attempt !== started) {
      // Signed out while the call was starting: end the server side with the key it started with.
      await api(`/calls/${encodeURIComponent(answer.ref)}/hangup`, {}, startKey).catch(() => {});
      return;
    }
    browserRef = answer.ref;
    await peer.setRemoteDescription({type: answer.type, sdp: answer.sdp});
    showCallControls(true);
    renderReadiness();
  } catch (error) {
    if (attempt !== started) { return; }  // sign-out already released the call
    release();
    browserRef = undefined;
    $('#start').disabled = false;
    if (error instanceof Unauthorized) { await signOut(`The key no longer works. ${KEY_HELP}`); return; }
    $('#call-status').textContent = error.message;
  }
};

async function hangUp() {
  const ref = browserRef;
  browserRef = undefined;
  callEnded();
  if (ref) { await api(`/calls/${encodeURIComponent(ref)}/hangup`, {}).catch(() => {}); }
}

$('#stop').onclick = hangUp;
$('#mute').onclick = () => {
  const muted = $('#mute').getAttribute('aria-pressed') !== 'true';
  setMuted(muted);
  $('#call-status').textContent = muted ? 'Microphone muted. The agent cannot hear you.' : 'Microphone on.';
};

// Outbound WhatsApp call ------------------------------------------------------------------

$('#recipient').oninput = () => { allowedRecipient = undefined; $('#dial').disabled = true; };

$('#permission').onclick = async () => {
  $('#outbound-status').textContent = 'Checking permission…';
  try {
    const result = await api('/outbound/permission', {recipient: $('#recipient').value});
    allowedRecipient = result.can_call ? $('#recipient').value : undefined;
    $('#dial').disabled = !result.can_call;
    $('#outbound-status').textContent = result.can_call ? 'WhatsApp allows this call.'
      : `WhatsApp does not allow this call yet (permission: ${result.permission_status}). Ask the person to allow calls first.`;
  } catch (error) { $('#outbound-status').textContent = error.message; }
};

$('#dial').onclick = async () => {
  if (allowedRecipient !== $('#recipient').value) { return; }
  const started = attempt, startKey = key;
  $('#dial').disabled = true;
  allowedRecipient = undefined;
  $('#outbound-status').textContent = 'Requesting the call…';
  try {
    const result = await api('/outbound/call', {recipient: $('#recipient').value});
    if (attempt !== started) {
      if (result.state === 'ringing') { await api(`/calls/${encodeURIComponent(result.ref)}/hangup`, {}, startKey).catch(() => {}); }
      return;
    }
    outboundRef = result.state === 'ringing' ? result.ref : undefined;
    $('#outbound-stop').disabled = !outboundRef;
    $('#outbound-status').textContent = outboundRef ? 'Ringing. Answer on the phone.' : 'The call ended.';
  } catch (error) { $('#outbound-status').textContent = error.message; }
};

$('#outbound-stop').onclick = async () => {
  $('#outbound-stop').disabled = true;
  try {
    await api(`/calls/${encodeURIComponent(outboundRef)}/hangup`, {});
    $('#outbound-status').textContent = 'Call ended.';
  } catch (error) { $('#outbound-status').textContent = error.message; }
  outboundRef = undefined;
};

document.addEventListener('visibilitychange', refresh);
if (key) { signIn(key, true); } else { showSignedIn(false); }
