// 페이지만 열고 나가는 봇이 GA와 Web Analytics에 집계되지 않게, 입력이나 10초 체류 뒤에만 불러옴. mongsil.dev, bori.wiki, Labs와 같은 방식
(function(){
if(location.hostname!=='bot.mongsil.dev'||navigator.webdriver)return;
var GA_ID='G-S3NS09MHSM',WA_TOKEN='b3ce5508d4784b2a847f3ecf26b02376';
var ev=['pointerdown','keydown','scroll','touchstart','mousemove'],timer,done=false;
function add(src,beacon){var s=document.createElement('script');s.async=true;s.src=src;if(beacon)s.setAttribute('data-cf-beacon',beacon);document.head.appendChild(s);}
function load(){
if(done)return;done=true;clearTimeout(timer);
ev.forEach(function(e){removeEventListener(e,load,true);});
document.removeEventListener('visibilitychange',arm);
window.dataLayer=window.dataLayer||[];window.gtag=function(){dataLayer.push(arguments);};
gtag('js',new Date());gtag('config',GA_ID);
add('https://www.googletagmanager.com/gtag/js?id='+GA_ID);
add('https://static.cloudflareinsights.com/beacon.min.js','{"token":"'+WA_TOKEN+'"}');
}
function arm(){clearTimeout(timer);if(document.visibilityState==='visible')timer=setTimeout(load,10000);}
ev.forEach(function(e){addEventListener(e,load,{capture:true,passive:true});});
document.addEventListener('visibilitychange',arm);arm();
})();
