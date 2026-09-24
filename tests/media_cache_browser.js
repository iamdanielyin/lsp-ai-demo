// Run after login to tests/browser_app.py using agent-browser eval --stdin.
(async()=>{
  const assert=(ok,message)=>{if(!ok)throw new Error(message);};
  const waitFor=async check=>{for(let i=0;i<100;i++){if(check())return;await new Promise(r=>setTimeout(r,50));}throw new Error('Media cache check timed out');};
  const mediaEntries=()=>performance.getEntriesByType('resource').filter(x=>x.name.includes('/media/'));
  const select=async id=>{
    document.querySelector(`.conversation-item[data-id="${id}"]`).click();
    await waitFor(()=>S.detail?.conversation.id===id);
  };
  const ready=()=>[...document.querySelectorAll('.chat-media')].every(b=>!b.disabled);
  const thumbnail=type=>document.querySelector(`article[title="消息 ID：LOCAL_TEST_MESSAGE_${type==='image'?'120':'121'}"] .chat-media`);
  clearInterval(S.poll);
  await select(1);await waitFor(ready);
  performance.clearResourceTimings();
  await select(3);await select(1);await waitFor(ready);
  for(const type of ['image','video']){
    for(let i=0;i<2;i++){
      const button=thumbnail(type);button.scrollIntoView();button.click();
      await waitFor(()=>type==='image'?document.querySelector('.gslide-image img')?.naturalWidth>0:document.querySelector('.viewer-video')?.readyState>=2);
      document.querySelector('.gclose').click();await waitFor(()=>!document.querySelector('.glightbox-container'));
    }
  }
  const previews=mediaEntries();
  assert(previews.every(x=>x.transferSize===0),'Switching/reopening downloaded cached media again');
  const attachment=document.querySelector('a.attachment').href;
  await (await fetch(attachment)).arrayBuffer();performance.clearResourceTimings();
  await (await fetch(attachment)).arrayBuffer();
  assert(mediaEntries().every(x=>x.transferSize===0),'Attachment was downloaded twice');
  performance.clearResourceTimings();
  // A different browser URL must miss the old cache; the backend source-version guard has unit coverage.
  const changed=attachment+'&cache_test='+Date.now();
  const response=await fetch(changed);assert(response.ok,'Changed URL failed');await response.arrayBuffer();
  assert(mediaEntries().some(x=>x.transferSize>0),'Changed URL reused an old cache entry');
  return {passed:true,repeatedPreviewRequests:previews.length,previewBytesTransferred:0,attachmentReused:true,changedUrlFetched:true};
})()
