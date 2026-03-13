/**
 * UniPi Agri HA - Node-RED Settings
 *
 * This file customizes Node-RED for the setup wizard functionality.
 */

module.exports = {
    // User directory
    userDir: '/data',

    // Flow file
    flowFile: 'flows.json',

    // Credential secret
    credentialSecret: process.env.NODE_RED_CREDENTIAL_SECRET || "unipi-agri-secret",

    // HTTP admin path
    httpAdminRoot: '/',

    // HTTP node path
    httpNodeRoot: '/',

    // UI path
    ui: { path: "ui" },

    // Function node settings - allow fs and child_process
    functionGlobalContext: {
        fs: require('fs'),
        os: require('os'),
        path: require('path')
    },

    // Allow external modules in function nodes
    functionExternalModules: true,

    // Logging
    logging: {
        console: {
            level: "info",
            metrics: false,
            audit: false
        }
    },

    // Editor theme
    editorTheme: {
        projects: {
            enabled: false
        },
        header: {
            title: "UniPi Agri HA"
        },
        page: {
            title: "UniPi Agri HA - Node-RED"
        }
    },

    // Context storage
    contextStorage: {
        default: {
            module: "memory"
        },
        file: {
            module: "localfilesystem"
        }
    },

    // Disable palette manager for security
    // paletteCategories: ['subflows', 'common', 'function', 'network', 'sequence', 'parser', 'storage'],

    // Auto-install modules
    autoInstallModules: true
};
