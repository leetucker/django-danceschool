/**
 * manage_register_cart.js
 *
 * At-the-door register extension for register_cart_common.js.
 * Adds item-button handling, voucher codes, customer/guest lookup, and
 * check-in.  Requires register_cart_common.js to be loaded first.
 *
 * Legacy button attributes translated to CartView item format:
 *   type="eventRegistration", event="<id>", roleId="<id>", dropIn="true/false"
 *   type="MerchItem", itemId="<id>", sku="<sku>"
 *
 * Items that already carry item_type / item_id / sku pass through unchanged.
 */
document.addEventListener('DOMContentLoaded', function () {

    const {
        cart, syncCart, addAlert, jsonFetch,
        fmt, toTitleCase, htmlToNodes, buildEventItem,
    } = window.registerCart;

    // ===== Item format translation =====
    // Translates the data attributes from .add-item buttons (legacy format used
    // by existing register plugin templates) into CartView item format.

    // Template data attributes use Python-style booleans ("True"/"False").
    // This helper matches both "True" and "true" (and the boolean true).
    function isTruthy(v) {
        return v === true || (typeof v === 'string' && v.toLowerCase() === 'true');
    }

    function translateButtonData(raw) {
        // Already in CartView format.
        if (raw.itemType || raw.item_type) {
            return {
                item_type: raw.itemType || raw.item_type,
                item_id: parseInt(raw.itemId || raw.item_id),
                sku: raw.sku,
                quantity: parseInt(raw.quantity) || 1,
                choiceId: raw.choiceId || null,
            };
        }

        if (raw.type === 'eventRegistration') {
            const item = buildEventItem(
                raw.event,
                raw.roleId,
                raw.quantity,
                raw.price ? parseFloat(raw.price) : null,
                {
                    dropIn: isTruthy(raw.dropIn),
                    dropInOccurrence: raw.dropInOccurrence || null,
                    // Cache the element ID so the door register can update
                    // the badge counter on the source button.
                    choiceId: raw.choiceId || null,
                    // Cache the event name so lookupDescription() can fall
                    // back to it when the SKU is not yet in the catalog.
                    description: raw.name || raw.description || null,
                }
            );
            // Door-only flags are not shared with the public register.
            if (raw.requireFull !== undefined) { item.requireFull = isTruthy(raw.requireFull); }
            if (raw.autoSubmit !== undefined) { item.autoSubmit = isTruthy(raw.autoSubmit); }
            if (raw.autoFulfill !== undefined) { item.autoFulfill = isTruthy(raw.autoFulfill); }
            if (raw.paymentMethod) { item.paymentMethod = raw.paymentMethod; }
            return item;
        }

        if (raw.type === 'MerchItem' || raw.type === 'merch' || raw.type === 'merchItem') {
            const item = {
                item_type: 'MerchItem',
                item_id: parseInt(raw.itemId),
                sku: raw.sku,
                quantity: parseInt(raw.quantity) || 1,
                choiceId: raw.choiceId || null,
                description: raw.description || null,
                price: raw.price ? parseFloat(raw.price) : null,
            };
            // Door-only flags are not shared with the public register.
            if (raw.requireFull !== undefined) { item.requireFull = isTruthy(raw.requireFull); }
            if (raw.autoSubmit !== undefined) { item.autoSubmit = isTruthy(raw.autoSubmit); }
            if (raw.autoFulfill !== undefined) { item.autoFulfill = isTruthy(raw.autoFulfill); }
            if (raw.paymentMethod) { item.paymentMethod = raw.paymentMethod; }
            return item;
        }

        // Unknown type — pass through as-is and let the server validate.
        return raw;
    }

    // ===== Event handlers =====

    // Add item to cart.
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.add-item');
        if (!btn) { return; }

        const raw = {};
        Object.keys(btn.dataset).forEach(function (k) { raw[k] = btn.dataset[k]; });

        // Embedded voucher code.
        if (raw.voucherId) {
            if (cart.discount_code && cart.discount_code !== raw.voucherId) {
                addAlert(regParams.multipleVoucherString);
                return;
            }
            cart.discount_code = raw.voucherId;
            delete raw.voucherId;
        }

        // Student flag: propagate to cart-level state so the server can
        // apply student-only discounts.
        if (raw.student === 'True' || raw.student === 'true' || raw.student === true) {
            cart.student = true;
        }

        const newItem = translateButtonData(raw);
        const existingIdx = cart.items.findIndex(function (i) { return i.sku === newItem.sku; });
        let updatedItems;

        if (existingIdx !== -1 && raw.updateQuantity === 'true') {
            updatedItems = cart.items.map(function (item, idx) {
                if (idx !== existingIdx) { return item; }
                return Object.assign({}, item, {
                    quantity: (item.quantity || 1) + (newItem.quantity || 1),
                });
            });
        } else if (existingIdx !== -1) {
            updatedItems = cart.items.map(function (item, idx) {
                return idx === existingIdx ? newItem : item;
            });
        } else {
            updatedItems = cart.items.concat([newItem]);
        }

        syncCart(updatedItems);
    });

    // Add voucher code.
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.add-voucher');
        if (!btn) { return; }

        let code = btn.dataset.id || '';
        if (!code) {
            const input = btn.closest('.voucherDetail') &&
                btn.closest('.voucherDetail').querySelector('input');
            if (input) { code = input.value; }
        }
        if (!code) { return; }

        if (cart.discount_code && cart.discount_code !== code) {
            addAlert(regParams.multipleVoucherString);
            return;
        }

        cart.discount_code = code;

        if (!cart.items || cart.items.length === 0) {
            addAlert(regParams.emptyRegisterVoucherString, 'alert-info');
        } else {
            syncCart(cart.items);
        }
    });

    // ===== Customer lookup =====

    const nameSelect = document.getElementById('id_name');
    // Use django.jQuery (= the jQuery instance that has Select2 registered on it
    // via DAL's media).  window.jQuery is restored by noConflict() to the
    // pre-form-media instance which does NOT have Select2, so event listeners
    // bound there never fire.
    if (nameSelect && window.django && window.django.jQuery) {
        const $ = window.django.jQuery;

        // Use select2:select (Select2 v4) which provides result data directly in
        // e.params.data.  The text field contains the HTML from get_result_label,
        // e.g. '<span data-id="123" data-type="Customer" ...>John Doe</span>'.
        // Parse it to read the custom data-* attributes.
        $(nameSelect).on('select2:select', function (e) {
            const labelHtml = e.params.data.text || '';
            const optData = labelHtml ? ($(labelHtml).data() || {}) : {};

            if (!optData.type) {
                clearCustomerPanels();
                return;
            }

            if (optData.type === regParams.customerString) {
                const payload = {
                    id: optData.id,
                    guestType: optData.type,
                    date: regParams.registerDate,
                    eventList: regParams.guestLookupEvents,
                };

                clearElement('guestInfoTable', 'tbody tr td');
                hideEl('guestInfoTable');
                clearElement('customerInfoTable', 'tbody');
                showEl('customerInfoCard');

                jsonFetch(regParams.customerLookupUrl, {
                    method: 'POST',
                    body: JSON.stringify(payload),
                })
                .then(function (r) { return r.json(); })
                .then(function (response) {
                    const table = document.getElementById('customerInfoTable');
                    const example = document.getElementById('customerInfoExample');
                    if (!table || !example) { return; }
                    table.classList.remove('d-none');
                    const tbody = table.querySelector('tbody');

                    response.forEach(function (entry) {
                        const row = example.querySelector('tr').cloneNode(true);
                        const checkId = 'checkIn_' + entry.id + '_' + entry.occurrenceId;
                        setCheckIn(row, '.customerCheckIn', checkId, {
                            checked: entry.checkedIn,
                            value: entry.id,
                            occurrenceId: entry.occurrenceId,
                            eventId: entry.event.id,
                        });

                        let statusString = entry.registration.invoice.statusLabel;
                        if (entry.registration.invoice.outstandingBalance > 0) {
                            statusString += ' (' + regParams.outstandingBalanceString +
                                ' ' + regParams.currencySymbol +
                                fmt(entry.registration.invoice.outstandingBalance) + ')';
                        }

                        let extrasText = '';
                        regExtrasFunctions.forEach(function (fn) { extrasText += fn(entry.extras); });
                        const extrasEl = row.querySelector('.customerInfoExtras');
                        if (extrasEl) { extrasEl.replaceChildren.apply(extrasEl, htmlToNodes(extrasText)); }

                        let roleHtml = '';
                        if (extrasText) { roleHtml += '<strong>' + regParams.roleString + ':</strong> '; }
                        if (entry.dropIn) { roleHtml += regParams.dropInString + ' '; }
                        if (entry.role && entry.role.name) { roleHtml += entry.role.name; }
                        const roleEl = row.querySelector('.customerInfoRole');
                        if (roleEl) { roleEl.innerHTML = roleHtml; }

                        setText(row, '.customerInfoEvent', entry.event.name);
                        setText(row, '.customerInfoTime',
                            new Date(entry.occurrenceStartTime)
                                .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                        );
                        setText(row, '.customerInfoStudent', toTitleCase(String(entry.student)));
                        setText(row, '.customerInfoPaymentStatus', statusString);
                        setHref(row, '.customerInvoiceLink', entry.registration.invoice.url);
                        setHref(row, '.customerRegistrationLink', entry.registration.url);

                        tbody.appendChild(row);
                    });
                });

            } else if (optData.type) {
                const payload = {
                    id: optData.id,
                    guestListId: optData.guestListId,
                    modelType: optData.modelType,
                    date: regParams.registerDate,
                    eventList: regParams.guestLookupEvents,
                    checkinType: 'O',
                };

                clearElement('customerInfoTable', 'tbody');
                hideEl('customerInfoTable');
                showEl('customerInfoCard');
                clearElement('guestInfoTable', 'tbody');

                jsonFetch(regParams.guestLookupUrl, {
                    method: 'POST',
                    body: JSON.stringify(payload),
                })
                .then(function (r) { return r.json(); })
                .then(function (response) {
                    const table = document.getElementById('guestInfoTable');
                    const example = document.getElementById('guestInfoExample');
                    if (!table || !example) { return; }
                    table.classList.remove('d-none');
                    const tbody = table.querySelector('tbody');

                    (response.events || []).forEach(function (entry) {
                        const row = example.querySelector('tr').cloneNode(true);
                        const checkId = 'checkIn_' + entry.id + '_' + entry.eventId;
                        setCheckIn(row, '.customerCheckIn', checkId, {
                            checked: entry.checkedIn,
                            value: entry.id,
                            firstName: entry.firstName,
                            lastName: entry.lastName,
                            modelType: entry.modelType,
                            eventId: entry.eventId,
                            occurrenceId: entry.occurrenceId,
                        });

                        setText(row, '.guestInfoEvent', entry.eventName);

                        const nameEl = row.querySelector('.guestInfoName');
                        if (nameEl) {
                            let nameHtml = entry.firstName + ' ' + entry.lastName;
                            if (entry.email) { nameHtml += '<br>' + entry.email; }
                            nameEl.innerHTML = nameHtml;
                        }

                        setText(row, '.guestInfoType', entry.guestType);

                        let extrasText = '';
                        regExtrasFunctions.forEach(function (fn) { extrasText += fn(entry.extras); });
                        const extrasEl = row.querySelector('.guestInfoExtras');
                        if (extrasEl) { extrasEl.replaceChildren.apply(extrasEl, htmlToNodes(extrasText)); }

                        tbody.appendChild(row);
                    });
                });
            }
        });

        // Clear the info panel when the selection is removed.
        $(nameSelect).on('select2:unselect select2:clear', function () {
            clearCustomerPanels();
        });
    }

    // ===== Check-in =====

    document.addEventListener('click', function (e) {
        const checkIn = e.target.closest('.customerCheckIn');
        if (!checkIn) { return; }

        checkIn.disabled = true;
        const initialStatus = !checkIn.checked;

        const requestData = {
            request: 'update',
            event_id: checkIn.dataset.eventId,
            checkin_type: 'O',
            occurrence_id: checkIn.dataset.occurrenceId,
        };

        if (checkIn.classList.contains('guestCheckIn')) {
            requestData.names = [{
                first_name: checkIn.dataset.firstName,
                last_name: checkIn.dataset.lastName,
                cancelled: !checkIn.checked,
            }];
        } else if (checkIn.classList.contains('registrationCheckIn')) {
            requestData.registrations = [{
                id: checkIn.value,
                cancelled: !checkIn.checked,
            }];
        }

        jsonFetch(regParams.checkInUrl, {
            method: 'POST',
            body: JSON.stringify(requestData),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            setTimeout(function () {
                document.querySelectorAll('.customerCheckIn,.guestCheckIn')
                    .forEach(function (el) { el.removeAttribute('disabled'); });
            }, 500);
            if (data.status !== 'success') {
                checkIn.checked = initialStatus;
            }
        })
        .catch(function () {
            setTimeout(function () {
                checkIn.checked = initialStatus;
                document.querySelectorAll('.customerCheckIn')
                    .forEach(function (el) { el.removeAttribute('disabled'); });
            }, 500);
        });
    });

    // ===== DOM helpers =====

    function clearCustomerPanels() {
        clearElement('guestInfoTable', 'tbody');
        hideEl('guestInfoTable');
        clearElement('customerInfoTable', 'tbody');
        hideEl('customerInfoTable');
    }

    function clearElement(tableId, selector) {
        const el = document.getElementById(tableId);
        if (!el) { return; }
        const target = selector ? el.querySelector(selector) : el;
        if (target) { target.innerHTML = ''; }
    }

    function hideEl(id) {
        const el = document.getElementById(id);
        if (el) { el.classList.add('d-none'); }
    }

    function showEl(id) {
        const el = document.getElementById(id);
        if (!el) { return; }
        el.classList.remove('collapse');
        el.classList.add('show');
    }

    function setText(row, selector, text) {
        const el = row.querySelector(selector);
        if (el) { el.textContent = text; }
    }

    function setHref(row, selector, href) {
        const el = row.querySelector(selector);
        if (el) { el.href = href; }
    }

    function setCheckIn(row, selector, checkId, data) {
        const checkIn = row.querySelector(selector);
        const label = row.querySelector(selector + 'Label');
        if (checkIn) {
            checkIn.setAttribute('id', checkId);
            checkIn.checked = data.checked || false;
            checkIn.value = data.value || '';
            Object.keys(data).forEach(function (k) {
                if (k !== 'checked' && k !== 'value') {
                    checkIn.dataset[k] = data[k];
                }
            });
        }
        if (label) { label.setAttribute('for', checkId); }
    }

});
