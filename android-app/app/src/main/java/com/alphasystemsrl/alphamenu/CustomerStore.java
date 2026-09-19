package com.alphasystemsrl.alphamenu;

import android.content.Context;
import org.json.JSONArray;
import org.json.JSONObject;

final class CustomerStore {
    private static final String PREFS = "alpha_menu_customers";
    private static final String KEY = "items";
    private static final String TOKEN = "call_relay_token";

    static void save(Context context, JSONArray customers) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString(KEY, customers == null ? "[]" : customers.toString()).apply();
    }

    static JSONObject find(Context context, String phone) {
        String wanted = digits(phone);
        if (wanted.length() < 6) return null;
        try {
            JSONArray items = new JSONArray(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .getString(KEY, "[]"));
            for (int i = 0; i < items.length(); i++) {
                JSONObject customer = items.optJSONObject(i);
                String saved = digits(customer == null ? "" : customer.optString("telefono"));
                if (saved.equals(wanted) || (saved.length() >= 8 && wanted.length() >= 8
                        && saved.substring(saved.length() - 8).equals(wanted.substring(wanted.length() - 8)))) {
                    return customer;
                }
            }
        } catch (Exception ignored) { }
        return null;
    }

    static void saveRelayToken(Context context, String token) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(TOKEN, token).apply();
    }

    static String relayToken(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(TOKEN, "");
    }

    static String digits(String value) {
        return value == null ? "" : value.replaceAll("\\D", "");
    }

    private CustomerStore() { }
}
