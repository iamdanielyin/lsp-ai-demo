// Run in the authenticated tests/browser_app.py page with agent-browser eval --stdin.
(async()=>{
  const assert=(ok,message)=>{if(!ok)throw new Error(message);};
  const data=await api('/api/conversations/1/messages');
  const leaves=parts=>parts.flatMap(p=>p.parts?leaves(p.parts):[p]);
  const parts=data.messages.flatMap(m=>leaves(m.parts));
  const root=document.createElement('div');root.className='bubble';document.body.append(root);
  const urls=[];
  const waitFor=async check=>{for(let i=0;i<100;i++){if(check())return;await new Promise(resolve=>setTimeout(resolve,50));}throw new Error('Media state timed out');};
  const mount=(type,url)=>{
    root.innerHTML=mediaHTML({type,url});
    const button=root.querySelector('.chat-media');
    root.querySelector('img')?.setAttribute('loading','eager');
    bindMediaPreviews(root);return button;
  };
  try {
    for(const type of ['image','video']){
      const response=await fetch(parts.find(p=>p.type===type).url);
      assert(response.ok,'Fixture media request failed');
      const url=URL.createObjectURL(await response.blob());urls.push(url);
      let button=mount(type,url);
      assert(button.classList.contains('is-loading')&&button.disabled&&button.getAttribute('aria-busy')==='true','Missing loading state');
      assert(button.getBoundingClientRect().height>=100,'Placeholder has no height');
      await waitFor(()=>!button.disabled);
      assert(!button.classList.contains('is-loading')&&button.getAttribute('aria-busy')==='false','Loading state did not clear');
      assert(getComputedStyle(button.querySelector('.media-placeholder')).display==='none','Placeholder covers loaded media');
      // Rebind an already loaded element to cover cached image/video readiness.
      button.classList.add('is-loading');button.disabled=true;bindMediaPreviews(root);
      assert(!button.disabled&&!button.classList.contains('is-loading'),'Cached media stuck loading');
      const invalid=URL.createObjectURL(new Blob(['invalid-media'],{type:type==='image'?'image/png':'video/mp4'}));urls.push(invalid);
      button=mount(type,invalid);
      await waitFor(()=>button.classList.contains('is-error'));
      assert(button.disabled&&button.getAttribute('aria-busy')==='false','Failure state remained busy');
      assert(button.textContent.includes('加载失败'),'Missing failure label');
      assert(getComputedStyle(button.querySelector('.media-spinner')).display==='none','Failure spinner still visible');
    }
    return {passed:true,checks:['image/video loading','reserved space','loaded state','cached media','failure state']};
  } finally {root.remove();urls.forEach(url=>URL.revokeObjectURL(url));}
})()
