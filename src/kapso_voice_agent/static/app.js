'use strict';
const $ = selector => document.querySelector(selector);
let token = sessionStorage.getItem('operatorToken') || '';
let peer, microphone, browserRef, outboundRef, allowedRecipient, state;

async function api(path, body) {
  const options = {headers: {Authorization: `Bearer ${token}`}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`/operator/api${path}`, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { throw new Error(data.detail || `HTTP ${response.status}`); }
  return data;
}

$('#save-token').onclick = () => {
  token = $('#token').value.trim();
  sessionStorage.setItem('operatorToken', token);
  $('#token').value = '';
  refresh();
};

function release() {
  if (microphone) { microphone.getTracks().forEach(track => track.stop()); }
  if (peer) { peer.close(); }
  microphone = peer = undefined;
}

$('#start').onclick = async () => {
  $('#start').disabled = true;
  $('#call-status').textContent = 'Connecting…';
  try {
    microphone = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    peer = new RTCPeerConnection();
    peer.ontrack = event => { $('#audio').srcObject = new MediaStream([event.track]); };
    peer.onconnectionstatechange = () => {
      if (peer && ['failed', 'closed', 'disconnected'].includes(peer.connectionState)) {
        release();
        browserRef = undefined;
        $('#call-status').textContent = 'Call ended';
      }
    };
    microphone.getTracks().forEach(track => peer.addTrack(track, microphone));
    await peer.setLocalDescription(await peer.createOffer());
    await new Promise((resolve, reject) => {
      if (peer.iceGatheringState === 'complete') { resolve(); return; }
      const timer = setTimeout(() => reject(new Error('ICE gathering timed out')), 10000);
      peer.onicegatheringstatechange = () => {
        if (peer && peer.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); }
      };
    });
    const answer = await api('/browser-call', {sdp: peer.localDescription.sdp});
    browserRef = answer.ref;
    await peer.setRemoteDescription({type: answer.type, sdp: answer.sdp});
    $('#stop').disabled = false;
    $('#call-status').textContent = 'On a call';
  } catch (error) {
    release();
    $('#call-status').textContent = error.message;
  }
};

$('#stop').onclick = async () => {
  const ref = browserRef;
  browserRef = undefined;
  release();
  $('#stop').disabled = true;
  $('#call-status').textContent = 'Call ended';
  if (ref) { await api(`/calls/${encodeURIComponent(ref)}/hangup`, {}).catch(() => {}); }
};

$('#recipient').oninput = () => { allowedRecipient = undefined; $('#dial').disabled = true; };

$('#permission').onclick = async () => {
  try {
    const result = await api('/outbound/permission', {recipient: $('#recipient').value});
    allowedRecipient = result.can_call ? $('#recipient').value : undefined;
    $('#dial').disabled = !result.can_call;
    $('#outbound-status').textContent = result.can_call ? 'WhatsApp allows this call.' : `Not allowed (permission: ${result.permission_status}).`;
  } catch (error) { $('#outbound-status').textContent = error.message; }
};

$('#dial').onclick = async () => {
  if (allowedRecipient !== $('#recipient').value) { return; }
  $('#dial').disabled = true;
  allowedRecipient = undefined;
  $('#outbound-status').textContent = 'Requesting the call…';
  try {
    const result = await api('/outbound/call', {recipient: $('#recipient').value});
    outboundRef = result.state === 'ringing' ? result.ref : undefined;
    $('#outbound-stop').disabled = !outboundRef;
    $('#outbound-status').textContent = outboundRef ? 'Ringing. Answer on the phone.' : 'The call ended.';
  } catch (error) { $('#outbound-status').textContent = error.message; }
};

$('#outbound-stop').onclick = async () => {
  $('#outbound-stop').disabled = true;
  try { await api(`/calls/${encodeURIComponent(outboundRef)}/hangup`, {}); $('#outbound-status').textContent = 'Call ended'; }
  catch (error) { $('#outbound-status').textContent = error.message; }
  outboundRef = undefined;
};

async function refresh() {
  if (!token) { $('#ready').textContent = ' Enter the token.'; return; }
  try { state = await api('/state'); }
  catch (error) { $('#ready').textContent = ` ${error.message}`; return; }
  $('#ready').textContent = state.calls_ready ? ' Ready for calls' : (state.agent_ready ? ' Browser tests only (Kapso settings missing)' : ` Agent not configured (missing ${state.agent_missing.join('; ')})`);
  $('#ready').textContent += ` · provider recording: ${state.recording.provider_recording}, local capture: ${state.recording.local_capture ? 'on' : 'off'}`;
  if (!peer) { $('#start').disabled = !state.agent_ready || state.busy; }
  $('#permission').disabled = !state.outbound_enabled;
  if (!state.outbound_enabled) { $('#dial').disabled = true; }
  if (outboundRef && !state.calls.some(call => call.ref === outboundRef)) {
    outboundRef = undefined;
    $('#outbound-stop').disabled = true;
    $('#outbound-status').textContent = 'Call ended';
  }
  $('#events').textContent = state.events.map(e => `${e.at.slice(11, 23)} ${e.event}${e.ref ? ' ' + e.ref : ''}`).join('\n');
}

refresh();
setInterval(refresh, 2000);
