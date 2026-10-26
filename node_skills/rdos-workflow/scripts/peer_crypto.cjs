'use strict';
// Fixed, dependency-free cryptography. No files, commands or network operations.
const crypto = require('node:crypto');
let input='';
process.stdin.setEncoding('utf8');
process.stdin.on('data', value => { input += value; if(input.length > 2_000_000) process.exit(2); });
process.stdin.on('end', () => {
  try {
    const request=JSON.parse(input); let result;
    if(request.action === 'generate') {
      const pair=crypto.generateKeyPairSync('ed25519');
      result={public_key:pair.publicKey.export({type:'spki',format:'pem'}),private_key:pair.privateKey.export({type:'pkcs8',format:'pem'})};
    } else if(request.action === 'sign') {
      result={signature:crypto.sign(null,Buffer.from(request.payload,'utf8'),request.private_key).toString('base64')};
    } else if(request.action === 'verify') {
      result={valid:crypto.verify(null,Buffer.from(request.payload,'utf8'),request.public_key,Buffer.from(request.signature,'base64'))};
    } else throw new Error('invalid_action');
    process.stdout.write(JSON.stringify(result));
  } catch (_) { process.stdout.write(JSON.stringify({error:'cryptography_failed'})); process.exitCode=1; }
});
