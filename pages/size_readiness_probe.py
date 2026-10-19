"""Read-only current-DOM metadata task; native disabled checks stay in Python."""

SIZE_READINESS_DOM = r"""(purchases, args) => {
    const norm = s => String(s || '').trim().replace(/\s+/g, ' ').toLowerCase();
    const purchase = purchases[0];
    const doc = purchase ? purchase.ownerDocument : document;
    const visible = el => {
        if (!el || !el.isConnected) return false;
        const style = getComputedStyle(el);
        if (style.display === 'contents') {
            return Array.from(el.childNodes).some(child => {
                if (child.nodeType === 1) return visible(child);
                if (child.nodeType !== 3) return false;
                const range = doc.createRange(); range.selectNode(child);
                const rect = range.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            });
        }
        // Match the installed Playwright visibility contract. WebKit uses
        // the closed-details fallback instead of its checkVisibility API.
        if (!args.webkit && el.checkVisibility) {
            if (!el.checkVisibility()) return false;
        } else {
            const details=el.closest('details,summary');
            if (details!==el && details && details.nodeName==='DETAILS' && !details.open) return false;
        }
        if (style.visibility !== 'visible') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const groupName = group => {
        const label = group.getAttribute('aria-label');
        if (label) return label.trim();
        const refs = group.getAttribute('aria-labelledby');
        if (refs) {
            const labels = refs.split(/\s+/).map(id => doc.getElementById(id)).filter(Boolean);
            if (labels.length) return labels.map(l => l.innerText.trim()).join(' ');
        }
        const legend = group.querySelector(':scope > legend');
        return legend ? legend.innerText.trim() : (group.getAttribute('name') || '').trim();
    };
    const associated = (group, radios) => {
        if (!purchase) return false;
        const id = purchase.getAttribute('id') || '';
        const form = group.parentElement && group.parentElement.closest('form');
        if (form && (!id || form.getAttribute('id') === id)) return true;
        if (!id) return false;
        return radios.some(r => r.getAttribute('form') === id ||
            (r.parentElement && r.parentElement.closest('form') && r.parentElement.closest('form').getAttribute('id') === id));
    };
    const models = args.models.filter(c => c.id && c.group_selector && c.option_selector)
        .map(c => ({config:c, groups:Array.from(doc.querySelectorAll(c.group_selector))}));
    const state = {size_model:null, size_group_detected:false, size_option_total:0,
        size_option_available:0, normal_size_available:0, custom_size_present:false,
        custom_measurement_model:null, selected_size:null,
        candidate_group_count:models.reduce((n,m) => n+m.groups.length,0),
        measurement_field_count:0, size_probe_status:'OK'};
    for (let mi=0; mi<models.length; mi++) {
        const {config,groups} = models[mi];
        for (let gi=0; gi<groups.length; gi++) {
            const group=groups[gi];
            if (!Object.entries(config.required_attributes || {}).every(([k,v]) => norm(group.getAttribute(k)) === norm(v))) continue;
            if (norm(config.expected_name) && norm(groupName(group)) !== norm(config.expected_name)) continue;
            const radios=Array.from(group.querySelectorAll(config.option_selector));
            if (!associated(group,radios)) continue;
            state.size_model=config.id; state.size_group_detected=true;
            state.size_option_total=radios.length;
            const custom=norm(config.custom_size_value || args.custom_value);
            const tokens=(config.disabled_class_tokens || []).map(norm).filter(Boolean);
            const blocked=el => el && tokens.some(t => (el.getAttribute('class') || '').toLowerCase().split(/\s+/).includes(t));
            const strategy=norm(config.control_strategy);
            const labels=Array.from(doc.querySelectorAll('label'));
            const candidates=[];
            for (let ri=0; ri<radios.length; ri++) {
                const radio=radios[ri], value=(radio.getAttribute('value') || '').trim();
                if (!value) continue;
                const isCustom=norm(value)===custom;
                state.custom_size_present ||= isCustom;
                if (radio.checked && state.selected_size===null) state.selected_size=value;
                const label=radio.closest('label') || (radio.id && labels.find(l => l.htmlFor===radio.id));
                let control;
                if (['radio','native'].includes(strategy)) control=visible(radio) ? radio : null;
                else if (['associated_label','label','label_for'].includes(strategy)) control=visible(label) ? label : visible(radio) ? radio : null;
                else control=visible(radio) ? radio : visible(label) ? label : null;
                if (!control || radio.matches(':disabled') || norm(radio.getAttribute('aria-disabled'))==='true' ||
                    blocked(radio) || blocked(radio.parentElement) || norm(control.getAttribute('aria-disabled'))==='true' || blocked(control)) continue;
                candidates.push({index:ri,custom:isCustom});
            }
            if (state.custom_size_present) state.custom_measurement_model=args.custom_model;
            return {state,candidates,model_index:mi,group_index:gi};
        }
    }
    return {state,candidates:[]};
}"""
