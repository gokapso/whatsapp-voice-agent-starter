'use strict';
// Operator console. The console access key (OPERATOR_TOKEN) is kept in sessionStorage for this
// tab only and sent as a bearer header; the page itself holds no secret.
const $ = selector => document.querySelector(selector);
const STORAGE = 'consoleAccessKey';
const clock = new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit'});
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
      ? (restoring ? 'Your saved key no longer works. The server may have a new key. Copy it again with uv run voice-agent console-key.'
        : 'This key does not match this server. Copy it again with uv run voice-agent console-key, then paste it here.')
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
  if (!restoring) { $('#status-title').focus(); }
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

// Setup status ----------------------------------------------------------------------------

function voiceName(voice) {
  if (voice.offline_fake_agent) { return `Offline fake agent (${voice.provider === 'grok' ? 'Grok' : 'ElevenLabs'} protocol)`; }
  return voice.provider === 'grok' ? 'Grok Voice' : 'ElevenLabs Agents';
}

function vendor(voice) {
  return voice.provider === 'grok' ? 'xAI' : 'ElevenLabs';
}

const PROBLEMS = {
  auth: voice => `${vendor(voice)} rejected the API key. Check ${voice.provider === 'grok' ? 'XAI_API_KEY' : 'ELEVENLABS_API_KEY'} in the server's .env, then restart the server.`,
  rate_limited: voice => `${vendor(voice)} is limiting requests for this key. Wait a minute, then start the call again.`,
  not_found: voice => (voice.provider === 'grok'
    ? `xAI could not open ${voice.model}. Check grok.model in agent/agent.toml and that your xAI team can use Grok Voice.`
    : 'ElevenLabs could not find the agent. Check ELEVENLABS_AGENT_ID, or run uv run voice-agent agent apply.'),
  unreachable: voice => `The server could not reach ${vendor(voice)}. Check its internet connection, then try again.`,
  failed: voice => `${vendor(voice)} ended the session before it was ready. Open the event log below for details, then try again.`,
};

const ICONS = {
  ok: '<path d="M20 6 9 17l-5-5"/>',
  todo: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5m0 3v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
};

function checkItem(kind, name, value, detail) {
  const item = document.createElement('li');
  item.className = `check ${kind}`;
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('class', 'icon check-icon');
  icon.setAttribute('aria-hidden', 'true');
  icon.innerHTML = ICONS[kind];
  const text = document.createElement('div');
  const title = document.createElement('p');
  title.className = 'check-title';
  const label = document.createElement('span');
  label.className = 'check-name';
  label.textContent = name;
  const status = document.createElement('span');
  status.className = 'check-state';
  status.textContent = value;
  title.append(label, status);
  text.append(title);
  if (detail) {
    const note = document.createElement('p');
    note.className = 'check-detail';
    note.textContent = detail;
    text.append(note);
  }
  item.append(icon, text);
  return item;
}

function renderChecklist() {
  const voice = state.voice;
  // Setting names only; the "(or DEV_AGENT_WS_URL)" development alternative is for the docs, not this card.
  const voiceMissing = state.agent_missing.filter(name => /^(XAI_|ELEVENLABS_)/.test(name)).map(name => name.replace(/ \(.*\)$/, ''));
  const items = [];
  const voiceValue = voice.provider === 'grok' && !voice.offline_fake_agent
    ? `Grok Voice · ${voice.model} · voice ${voice.voice}` : voiceName(voice);
  items.push(voiceMissing.length
    ? checkItem('todo', 'Voice provider', `${voiceValue}: not set up`, `Add ${voiceMissing.join(' and ')}. Run uv run voice-agent setup, then restart the server.`)
    : checkItem('ok', 'Voice provider', voiceValue,
      voice.offline_fake_agent ? 'Development stand-in: a tone and an echo, not a conversation.' : ''));
  if (state.calendar === 'calcom') {
    items.push(checkItem('ok', 'Calendar', 'Cal.com', 'Test bookings are real bookings in your calendar.'));
  } else if (state.calendar === 'local') {
    items.push(checkItem('ok', 'Calendar', 'Local development calendar', 'Bookings stay on this server. Use Cal.com for real callers.'));
  } else {
    items.push(checkItem('todo', 'Calendar', 'Not set up', 'Add CAL_API_KEY, or set CALENDAR=local to test without a calendar.'));
  }
  const missingKey = state.agent_missing.find(name => name.startsWith('CALLER_KEY_SECRET'));
  if (missingKey) {
    items.push(checkItem('todo', 'Caller key', 'Not set up', 'Add CALLER_KEY_SECRET. uv run voice-agent setup creates one.'));
  }
  const recorded = state.recording.next_greeting_says_recorded;
  items.push(recorded === null
    ? checkItem('todo', 'Recording notice', 'Not known yet', 'No call starts until the provider’s recording setting can be read.')
    : checkItem(recorded ? 'ok' : 'info', 'Recording notice', recorded ? 'Callers hear “This call is recorded.”' : 'No recording notice',
      recorded && voice.provider === 'grok' && !voice.offline_fake_agent ? 'xAI keeps API traffic for 30 days.' : ''));
  items.push(state.calls_ready
    ? checkItem('ok', 'WhatsApp', 'Connected through Kapso', '')
    : checkItem('info', 'WhatsApp', 'Not connected', 'Browser test calls work without it. See docs/deploy.md to connect a number.'));
  $('#checklist').replaceChildren(...items);
  $('#status-summary').textContent = state.agent_ready ? 'Ready for test calls.' : 'Finish the items marked below to start a test call.';

  const notice = $('#provider-problem');
  const problem = voice.problem;
  notice.hidden = !problem;
  notice.textContent = problem
    ? `Last attempt, ${clock.format(new Date(problem.at))}: ${(PROBLEMS[problem.problem] || PROBLEMS.failed)(voice)}` : '';
}

function renderBrowserCard() {
  const voice = state.voice;
  const calendar = state.calendar === 'calcom' ? 'your Cal.com calendar' : 'the local development calendar';
  if (voice.offline_fake_agent) {
    $('#browser-cost').textContent = 'Offline. No AI and no provider cost.';
    $('#browser-proves').textContent = 'You hear a short tone, then your own voice. This checks your microphone, the audio path and one calendar lookup.';
  } else {
    $('#browser-cost').textContent = voice.provider === 'grok'
      ? `Starts a real Grok Voice session (${voice.model}). xAI bills it per minute.`
      : 'Starts a real ElevenLabs Agents conversation. ElevenLabs bills it per minute.';
    $('#browser-proves').textContent = `Talk to the agent as a caller would: its voice, its answers and bookings in ${calendar}. It does not test the WhatsApp audio path.`;
  }
  if (!peer) {
    $('#start').disabled = !state.agent_ready || state.busy;
    if (!state.agent_ready && !$('#call-status').textContent) {
      $('#call-status').textContent = 'Finish the setup above to start a test call.';
    }
  }
}

function renderOutbound() {
  $('#outbound-off').hidden = state.outbound_enabled;
  $('#outbound-form').hidden = !state.outbound_enabled;
  if (outboundRef && !state.calls.some(call => call.ref === outboundRef)) {
    outboundRef = undefined;
    $('#outbound-stop').disabled = true;
    $('#outbound-status').textContent = 'Call ended.';
  }
}

function render() {
  renderChecklist();
  renderBrowserCard();
  renderOutbound();
  $('#footer-provider').textContent = voiceName(state.voice);
  $('#events').textContent = state.events.map(e => `${e.at.slice(11, 23)} ${e.event}${e.ref ? ' ' + e.ref : ''}${e.media ? ' media=' + e.media : ''}`).join('\n');
}

async function refresh() {
  if (!key || document.hidden) { return; }
  try {
    state = await api('/state');
  } catch (error) {
    if (error instanceof Unauthorized) {
      await signOut('Your key no longer works. The server may have a new key. Copy it again with uv run voice-agent console-key.');
    } else {
      $('#status-summary').textContent = 'Unable to reach the server. Retrying every 2 seconds.';
    }
    return;
  }
  render();
}

// Browser test call -----------------------------------------------------------------------

function elapsed() {
  const seconds = Math.floor((Date.now() - callStartedAt) / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function release() {
  if (microphone) { microphone.getTracks().forEach(track => track.stop()); }
  if (peer) { peer.close(); }
  microphone = peer = undefined;
  clearInterval(timer);
  timer = undefined;
  $('#timer').textContent = '';
  $('#stop').disabled = true;
}

function callEnded(message) {
  const duration = callStartedAt ? elapsed() : '';
  callStartedAt = undefined;
  release();
  browserRef = undefined;
  $('#call-status').textContent = message || (duration ? `Call ended after ${duration}.` : 'Call ended.');
  if (state) { $('#start').disabled = !state.agent_ready; }
  refresh();
}

function microphoneMessage(error) {
  if (error.name === 'NotAllowedError') {
    return 'Microphone access is blocked. Allow it for this site in your browser settings, then start the call again.';
  }
  if (error.name === 'NotFoundError') { return 'No microphone found. Connect one, then start the call again.'; }
  return 'Unable to use the microphone. Check your browser settings, then try again.';
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
        timer = setInterval(() => { $('#timer').textContent = elapsed(); }, 1000);
      } else if (['failed', 'closed', 'disconnected'].includes(peer.connectionState)) {
        callEnded(peer.connectionState === 'failed' && !callStartedAt
          ? 'The audio could not connect. If the server is remote, it needs a TURN relay (docs/deploy.md).' : '');
      }
    };
    microphone.getTracks().forEach(track => peer.addTrack(track, microphone));
    await peer.setLocalDescription(await peer.createOffer());
    await new Promise((resolve, reject) => {
      if (peer.iceGatheringState === 'complete') { resolve(); return; }
      const wait = setTimeout(() => reject(new Error('Your browser took too long to prepare the audio connection. Try again.')), 10000);
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
    $('#stop').disabled = false;
  } catch (error) {
    if (attempt !== started) { return; }  // sign-out already released the call
    release();
    browserRef = undefined;
    $('#start').disabled = false;
    if (error instanceof Unauthorized) { await signOut('Your key no longer works. Sign in again.'); return; }
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
