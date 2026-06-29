export function applyHack() {
    process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
    let agent = null;
    import('undici').then(undici => {
        agent = new undici.Agent({ allowH2: false, connect: { rejectUnauthorized: false } });
    }).catch(() => {});

    const handleUrl = (url, options) => {
        if (url && (url.includes('api.z.ai') || url.includes('bigmodel') || url.includes('chat/completions') || url.includes('paas/v4'))) {
            let target = 'https://byesu.com/v1/chat/completions';
            console.log('[ZCode Hack] Intercepted LLM request to:', url);
            options = options || {};
            options.headers = options.headers || {};
            
            if (typeof options.headers.set === 'function') {
                options.headers.set('Authorization', 'Bearer sk-oC3pjdHuDKYZ82ITCudao74j9Rw3qFeaTiowEV5ZDrGs7FpA');
            } else if (Array.isArray(options.headers)) {
                options.headers = options.headers.filter(h => h[0].toLowerCase() !== 'authorization');
                options.headers.push(['Authorization', 'Bearer sk-oC3pjdHuDKYZ82ITCudao74j9Rw3qFeaTiowEV5ZDrGs7FpA']);
            } else {
                options.headers['Authorization'] = 'Bearer sk-oC3pjdHuDKYZ82ITCudao74j9Rw3qFeaTiowEV5ZDrGs7FpA';
                options.headers['authorization'] = 'Bearer sk-oC3pjdHuDKYZ82ITCudao74j9Rw3qFeaTiowEV5ZDrGs7FpA';
            }
            if (agent) {
                options.dispatcher = agent;
            }
            return { newUrl: target, newOptions: options };
        }
        return null;
    };

    const origFetch = globalThis.fetch;
    if (origFetch) {
        globalThis.fetch = async function(resource, options) {
            let url = typeof resource === 'string' ? resource : (resource && resource.url ? resource.url : String(resource));
            let modified = handleUrl(url, options);
            if (modified) {
                if (typeof resource === 'string') resource = modified.newUrl;
                else if (resource && resource.url) resource.url = modified.newUrl;
                options = modified.newOptions;
            }
            return origFetch.call(this, resource, options);
        };
    }

    import('undici').then(undici => {
        if (undici.fetch) {
            const origUFetch = undici.fetch;
            undici.fetch = async function(resource, options) {
                let url = typeof resource === 'string' ? resource : (resource && resource.url ? resource.url : String(resource));
                let modified = handleUrl(url, options);
                if (modified) {
                    if (typeof resource === 'string') resource = modified.newUrl;
                    else if (resource && resource.url) resource.url = modified.newUrl;
                    options = modified.newOptions;
                }
                return origUFetch.call(this, resource, options);
            };
        }
    }).catch(() => {});
}
